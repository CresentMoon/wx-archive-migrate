#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sweep_any_reserve.py — 从进程内存转储里扫出 SQLCipher 的**派生密钥**（与 reserve 无关）。

核心洞察
--------
SQLCipher 的库密钥分两层：磁盘上没有任何口令，但**派生出来的 AES 密钥必然在内存里**
（库被打开着的期间）。验证一个候选密钥不需要跑 64000 轮 PBKDF2 —— 只要用它解密
**一个 AES 块**，看结果是不是 SQLite 的页头：

    派生密钥验证 ≈ 1 次 AES 块解密（~微秒级）
    跑一遍 PBKDF2      ≈ 16 ms
    ⇒ 快约 16000 倍

所以"整块内存按 4 字节对齐穷举"是可行的（几亿候选 / 16 核 ≈ 90 秒）。

为什么是「与 reserve 无关」
---------------------------
最直观的判据要用到页 1 的 IV，而 **IV 的位置 = `PAGE - reserve`**，也就是要先猜中
reserve（每页保留段长度）。猜错就全盘漏扫 —— 我们为此白跑过一整轮。

本工具改用 **CBC 的第二个密文块**，它天然不含 IV：

    P1 = D(C1) XOR IV
    P2 = D(C2) XOR C1        <-- 不需要 IV，也就不需要 reserve

`P2` 对应 SQLite 页 1 的 [32:48]：

    [32:36] freelist 头页号（通常 0）
    [36:40] freelist 总页数（通常 0）
    [40:44] schema cookie（任意）
    [44:48] schema format number（大端，1..4）

要求 [44:48] 形如 `00 00 00 01..04` —— 一个约 2^-30 的过滤器，成本仍然只有一次块解密。

命中之后**再**用候选 reserve 去解第一个块、确认页头：

    10 00 <读版本> <写版本> <reserve> 40 20 20

这里**必须同时接受两种页头**：

    10 00 01 01 <res> 40 20 20   回滚日志模式（2.x 就是这样）
    10 00 02 02 <res> 40 20 20   **WAL 模式（3.9 自己写出来的库全是这个）**

只认前者会**整代漏扫**（我们踩过）。顺带：确认这一步还能把真正的 reserve 反推出来。

⚠️ Windows 上的一个致命陷阱
---------------------------
`multiprocessing` 在 Windows 上是 **spawn**：子进程会重新 import 模块。
所以「父进程给全局变量赋值、worker 里读」是**错的** —— worker 看到的是模块级默认值，
**实际什么都没测**，却因为磁盘 I/O 消耗了时间，伪装成"扫完了"。
所有跨进程数据**必须**走 `initializer`。
因此本工具把自检做成**强制门**：真实扫描前会自动跑一次注入式自检，不过就拒绝扫描。
（"10 秒扫完 477 MB"不可能是真的 —— **性能异常本身就是 bug 信号**。）

依赖
----
热路径用 ctypes 直接调 libcrypto 的裸 AES（比对象化调用快约 3 倍）。
自检用 `cryptography` 造合成页（只有自检需要它）。
找不到 libcrypto 时会明确报错 —— Windows 的 `cryptography` wheel 里是静态 OpenSSL，
没有可加载的 libcrypto，那种环境请改在 WSL/Linux 里跑扫描。

用法
----
    python3 sweep_any_reserve.py --selftest
    python3 sweep_any_reserve.py --dump <DUMP> --target <MSG_DB> [--target <MSG_DB> ...] \\
            [--reserve 48,80] [--jobs 16]

    --dump <文件>         dump_mem.py 产出的扁平内存转储
    --target <库>         目标库，可重复（每个库的派生密钥不同，定位到任意一个就够用）
    --reserve <列表>      确认页头时试哪些 reserve，默认 48,80
    --jobs <N>            并行进程数，默认 CPU 数
    --chunk-mb <N>        每个任务块大小（MB），默认 4
    --selftest            只跑注入式自检
    --skip-selftest       跳过真实扫描前的自动自检（**结果自负**）
"""
import argparse
import ctypes
import multiprocessing as mp
import os
import sys
import time

PAGE = 4096
HEADER_TAIL = bytes([0x40, 0x20, 0x20])      # payload fractions 64/32/32
LIB_CANDIDATES = ('libcrypto.so.3', 'libcrypto.so.1.1', 'libcrypto.so',
                  'libcrypto.3.dylib', 'libcrypto.dylib',
                  'libcrypto-3-x64.dll', 'libcrypto-1_1-x64.dll', 'libeay32.dll')
SKIP = 77                        # 自检退出码：缺依赖 / 平台不支持

_lib = None
_ks = ctypes.create_string_buffer(512)
_out = ctypes.create_string_buffer(16)

# ---- 跨进程数据只走 initializer（见 docstring 的 spawn 陷阱）----
_T = {}
_DUMP = None

USAGE = """用法：
    sweep_any_reserve.py --selftest
    sweep_any_reserve.py --dump <DUMP> --target <MSG_DB> [--target ...] [选项]

    --dump <文件>     dump_mem.py 产出的扁平内存转储（必需）
    --target <库>     SQLCipher 目标库，可重复（必需）
    --reserve <列表>  确认页头时试哪些 reserve，默认 48,80
    --jobs <N>        并行进程数，默认 CPU 数
    --chunk-mb <N>    任务块大小 MB，默认 4
    --selftest        只跑注入式自检
    --skip-selftest   跳过真实扫描前的自动自检（结果自负）

自检不 PASSED 就不要相信任何扫描结果：
Windows 的 spawn 会让"父进程赋全局变量"的写法**空跑**，看起来却像扫完了。"""


def load_lib():
    """加载 libcrypto 并准备好裸 AES 函数。"""
    global _lib
    if _lib is not None:
        return _lib
    err = []
    for name in LIB_CANDIDATES:
        try:
            lib = ctypes.CDLL(name)
        except OSError as e:                       # noqa: PERF203
            err.append('%s: %s' % (name, e))
            continue
        try:
            lib.AES_set_decrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
            lib.AES_decrypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
            lib.AES_decrypt.restype = None
        except AttributeError:
            err.append('%s: 没有 AES_set_decrypt_key/AES_decrypt' % name)
            continue
        _lib = lib
        return _lib
    raise SystemExit('加载不了 libcrypto（试过：%s）。\n%s\n'
                     '提示：Windows 的 cryptography wheel 是静态 OpenSSL，没有可加载的 '
                     'libcrypto —— 请在 WSL/Linux 里跑扫描。'
                     % (', '.join(LIB_CANDIDATES), '\n'.join('  ' + e for e in err)))


# ---------------------------------------------------------------- 目标
def make_target(path, reserves):
    """读出确认所需的全部字节：C1 / C2 / 各候选 reserve 的 IV。"""
    with open(path, 'rb') as f:
        head = f.read(PAGE)
    if len(head) < PAGE:
        return None
    ivs = {}
    for r in reserves:
        off = PAGE - r
        if off < 16:
            continue
        ivs[r] = head[off:off + 16]
    return (head[16:32], head[32:48], ivs)


def build_targets(paths, reserves):
    out = {}
    for p in paths:
        if not os.path.exists(p):
            print('  ！跳过（不存在）：%s' % p)
            continue
        t = make_target(p, reserves)
        if t:
            out[os.path.basename(p)] = t
    return out


def header_ok(p1, reserve):
    """页 1 头 8 字节。**同时接受回滚日志(01 01)与 WAL(02 02)** —— 只认前者会整代漏扫。"""
    return (p1[0] == 0x10 and p1[1] == 0x00
            and p1[2] in (1, 2) and p1[3] in (1, 2)
            and p1[4] == reserve
            and p1[5:8] == HEADER_TAIL)


def init(dump, targets):
    """worker 的一切状态都从这里进 —— spawn 下父进程的全局变量是看不到的。

    **libcrypto 的句柄也算"状态"**：父进程里 `load_lib()` 设的 `_lib` 在子进程里是
    None（子进程会重新 import 本模块）。这一点我们自己踩过：worker 里
    `_lib.AES_set_decrypt_key` 直接抛 `AttributeError: 'NoneType'`。
    既然 docstring 里立了"跨进程数据只走 initializer"的规矩，就得连库句柄一起走。
    """
    global _DUMP
    load_lib()
    _DUMP = dump
    _T.clear()
    _T.update(targets)


def worker(args):
    start, end = args
    lib = load_lib()                       # 双保险：本进程里没加载过就现在加载
    with open(_DUMP, 'rb') as f:
        f.seek(start)
        buf = f.read(end - start + 32)
    n = len(buf) - 32
    items = list(_T.items())
    setkey = lib.AES_set_decrypt_key
    dec1 = lib.AES_decrypt
    ks, out = _ks, _out
    hits = []
    for i in range(0, n, 4):
        cand = buf[i:i + 32]
        setkey(cand, 256, ks)
        for name, (c1, c2, ivs) in items:
            dec1(c2, out, ks)
            r2 = out.raw
            p2 = bytes(a ^ b for a, b in zip(r2, c1))
            # 与 reserve 无关的判据：schema format number 必须是 00 00 00 01..04
            if p2[12] or p2[13] or p2[14] or not (1 <= p2[15] <= 4):
                continue
            dec1(c1, out, ks)
            d1 = out.raw
            confirmed = None
            for r, iv in sorted(ivs.items()):
                p1 = bytes(a ^ b for a, b in zip(d1, iv))
                if header_ok(p1, r):
                    confirmed = (r, 'WAL' if p1[2] == 2 else 'rollback')
                    break
            hits.append((start + i, name, cand.hex(), confirmed,
                         int.from_bytes(p2[0:4], 'big'), int.from_bytes(p2[4:8], 'big'),
                         int.from_bytes(p2[8:12], 'big'), p2[15]))
    return hits


# ---------------------------------------------------------------- 自检
def selftest(jobs=None, chunk_mb=4):
    """注入式自检：把一段随机 32 字节当已知密钥塞进临时文件，看扫描器能否精确命中。

    **必须先 PASSED 才相信任何扫描结果**（原因见 docstring 的 spawn 陷阱）。

    缺 libcrypto（Windows 上必然缺）或 cryptography 时返回 `SKIP`(77)：
    那是**平台 / 依赖限制，不是缺陷** —— 别把它当成「自检失败」。
    """
    try:
        load_lib()
    except SystemExit as e:
        print('SKIP: %s' % e)
        return SKIP
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes    # noqa: PLC0415
    except ImportError:
        print('SKIP: 自检需要 cryptography（pip install cryptography）')
        return SKIP

    reserve = 80
    key = os.urandom(32)
    iv = os.urandom(16)
    p1 = bytes([0x10, 0x00, 0x02, 0x02, reserve]) + HEADER_TAIL + os.urandom(8)
    p2 = bytes(8) + os.urandom(4) + bytes([0, 0, 0, 4])

    def ecb(blk, k):
        e = Cipher(algorithms.AES(k), modes.ECB()).encryptor()
        return e.update(blk) + e.finalize()

    xor = lambda a, b: bytes(x ^ y for x, y in zip(a, b))       # noqa: E731
    c1 = ecb(xor(p1, iv), key)
    c2 = ecb(xor(p2, c1), key)

    off = 0x123454 & ~3
    blob = bytearray(os.urandom(6 << 20))
    blob[off:off + 32] = key
    path = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'sweep_selftest.bin')
    with open(path, 'wb') as f:
        f.write(bytes(blob))
    print('selftest：把注入密钥放在 0x%x（%s，%.1f MB）' % (off, path, len(blob) / 1048576.0))

    targets = {'selftest': (c1, c2, {reserve: iv})}
    chunk = chunk_mb << 20
    ranges = [(o, min(o + chunk, len(blob))) for o in range(0, len(blob), chunk)]
    hits = []
    with mp.Pool(jobs or os.cpu_count(), initializer=init, initargs=(path, targets)) as pool:
        for r in pool.imap_unordered(worker, ranges, chunksize=1):
            hits.extend(r)
    os.unlink(path)

    ok = any(h[0] == off and h[1] == 'selftest' and h[2] == key.hex()
             and h[3] and h[3][0] == reserve for h in hits)
    for h in hits[:5]:
        print('  命中 off=0x%08x target=%s reserve=%s(%s) freelist=%d/%d cookie=%d fmt=%d'
              % (h[0], h[1], h[3][0] if h[3] else None, h[3][1] if h[3] else '未确认',
                 h[4], h[5], h[6], h[7]))
    print('SELFTEST %s' % ('PASSED' if ok else 'FAILED'))
    return 0 if ok else 1


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    p = argparse.ArgumentParser(prog='sweep_any_reserve.py',
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description=USAGE)
    p.add_argument('--dump', default='', help='内存转储文件')
    p.add_argument('--target', action='append', default=[], help='目标库（可重复）')
    p.add_argument('--reserve', default='48,80', help='确认页头时试的 reserve 列表')
    p.add_argument('--jobs', type=int, default=0, help='并行进程数（默认 CPU 数）')
    p.add_argument('--chunk-mb', type=int, default=4, help='任务块大小 MB（默认 4）')
    p.add_argument('--selftest', action='store_true', help='只跑注入式自检')
    p.add_argument('--skip-selftest', action='store_true',
                   help='跳过真实扫描前的自动自检（结果自负）')
    args = p.parse_args(argv)

    jobs = args.jobs or os.cpu_count()

    if args.selftest:
        return selftest(jobs, args.chunk_mb)

    load_lib()

    if not args.dump or not os.path.exists(args.dump):
        print('需要 --dump <内存转储文件>')
        return 2
    if not args.target:
        print('需要至少一个 --target <库>')
        return 2

    # 强制门：真实扫描前先自检。空跑的扫描比不扫更糟 —— 它会让你以为试过了。
    if not args.skip_selftest:
        print('== 先跑注入式自检（用 --skip-selftest 可跳过，但结果自负）==')
        if selftest(jobs, args.chunk_mb) != 0:
            print('！！ 自检没有 PASSED —— 拒绝扫描。')
            return 3
        print()

    reserves = [int(x) for x in args.reserve.split(',') if x.strip()]
    targets = build_targets(args.target, reserves)
    size = os.path.getsize(args.dump)
    print('dump      : %s（%.1f MB）' % (args.dump, size / 1048576.0))
    print('targets   : %d 个 -> %s' % (len(targets), sorted(targets)))
    print('reserve   : %r（只用于"确认页头"，扫描本身与它无关）' % (reserves,))
    print('jobs      : %d' % jobs)
    if not targets:
        return 2

    chunk = args.chunk_mb << 20
    ranges = [(o, min(o + chunk, size)) for o in range(0, size, chunk)]
    t0 = time.time()
    hits = []
    with mp.Pool(jobs, initializer=init, initargs=(args.dump, targets)) as pool:
        for r in pool.imap_unordered(worker, ranges, chunksize=1):
            hits.extend(r)
    dt = time.time() - t0

    print('\n%.0f s on %d cores，%d 个候选' % (dt, jobs, len(hits)))
    for off, name, k, conf, fl_trunk, fl_cnt, cookie, fmt in sorted(hits, key=lambda h: h[0]):
        tag = ('reserve=%d(%s)' % (conf[0], conf[1])) if conf else 'reserve 未确认（不在 --reserve 里？）'
        print('  HIT %-22s off=0x%08x  key=%s  %s  freelist=%d/%d cookie=%d fmt=%d'
              % (name, off, k, tag, fl_trunk, fl_cnt, cookie, fmt))
    if not hits:
        print('  没有命中。')
        print('  排查顺序：① 库真的被客户端打开过吗（未打开的库没有派生密钥在内存里）；')
        print('           ② 转储是否完整；③ 目标库选对了吗；④ 先跑 --selftest 再确认管线没问题。')
    else:
        print('\n提醒：命中只说明"这个 32 字节值能解开某个目标库的页 1"。')
        print('      装库前请再用完整实现做一次全页 HMAC + integrity_check。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
