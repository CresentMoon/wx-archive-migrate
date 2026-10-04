#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从进程内存转储里扫出 2.x SQLCipher 库的**派生密钥**。

原理：把转储里每个 4 字节对齐的 32 字节窗口都当成候选的派生 AES-256 密钥，用它试解
某个目标库 page 1 的第一个密文块，看能不能得到合法的 SQLite 头。验证一把候选只要
一次 AES 密钥编排 + 一次块解密（约 1 us），而不是 64000 轮 PBKDF2（约 25 ms），
所以整份几百 MB 的转储扫一遍很便宜。

用法：
    python3 sweep.py --dump ./dump/mem_<pid>.bin --db-dir /path/Msg
    python3 sweep.py --dump ./dump/mem_<pid>.bin --target chatmsg=/path/ChatMsg.db
    python3 sweep.py --selftest

交叉验证：同一批库的**两个副本**（客户端数据根下的活副本、未动过的归档副本）各跑一遍，
两边应当给出**逐字节相同**的派生密钥 —— 这是分辨「真命中」与「碰巧」的办法。
用 `--target` 给两边各自的 tag 前缀即可一次跑完。

环境变量（对应参数未给出时作为兜底）：
    DUMP         内存转储文件（默认 ./dump/mem_<pid>.bin）
    WX_MSG_DIR   目标库所在目录（默认 Msg）

退出码：0 = 跑完（有没有命中看 stdout 的命中列表）；1 = 输入不完整；
        2 = 参数错误；77 = 自检跳过（缺 libcrypto）。
"""
import argparse
import ctypes
import ctypes.util
import multiprocessing as mp
import os
import random
import sys
import tempfile
import time

SKIP = 77
HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK = 4 << 20
PAGE, RESERVE = 4096, 48

# 2.x 的 8 个库。每个库是一个独立目标，但**一次 AES 密钥编排服务所有目标**，
# 所以多挂一个库只多一次块解密（约 0.05 us），几乎免费。
DB_NAMES = (
    'ChatMsg.db', 'MicroMsg.db', 'Media.db', 'Misc.db',
    'Favorite.db', 'BizChat.db', 'BizChatMsg.db', 'Emotion.db',
)

_T = {}
_DUMP = None
_lib = None
_ks = None
_out = None


LIB_CANDIDATES = ('libcrypto.so.3', 'libcrypto.so.1.1', 'libcrypto.so',
                  'libcrypto.3.dylib', 'libcrypto.dylib',
                  'libcrypto-3-x64.dll', 'libcrypto-1_1-x64.dll', 'libeay32.dll')


def get_lib():
    """懒加载 libcrypto —— **不要**放在模块顶层，否则没装它的机器上连 `--help` 都看不到。

    Windows 上基本加载不到：`cryptography` 的 wheel 用的是**静态** OpenSSL，
    没有可加载的 libcrypto ⇒ 扫描要在 WSL/Linux 里跑（与 `sweep_any_reserve.py` 同结论，
    那里也是专门试过 DLL 名之后才写下的）。
    """
    global _lib, _ks, _out
    if _lib is not None:
        return _lib
    err = []
    for cand in LIB_CANDIDATES:
        try:
            lib = ctypes.CDLL(cand)
            break
        except OSError as e:
            err.append('%s: %s' % (cand, e))
    else:
        raise SystemExit('加载不了 libcrypto（试过：%s）。\n%s\n'
                         '提示：Windows 的 cryptography wheel 是静态 OpenSSL，没有可加载的 '
                         'libcrypto —— 请在 WSL/Linux 里跑扫描。'
                         % (', '.join(LIB_CANDIDATES), '\n'.join('  ' + e for e in err)))
    lib.AES_set_decrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.AES_set_decrypt_key.restype = ctypes.c_int
    lib.AES_decrypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
    lib.AES_decrypt.restype = None
    lib.AES_set_encrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.AES_set_encrypt_key.restype = ctypes.c_int
    lib.AES_encrypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
    lib.AES_encrypt.restype = None
    _lib, _ks, _out = lib, ctypes.create_string_buffer(512), ctypes.create_string_buffer(16)
    return _lib


def try_get_lib():
    """自检专用：本平台加载不到 libcrypto 时返回 None。

    这是**平台限制，不是缺陷** —— 自检应当报 SKIP(77) 而不是 FAIL。
    """
    try:
        return get_lib()
    except SystemExit as e:
        print('SKIP: %s' % e)
        return None


def init(dump, targets):
    global _DUMP
    _DUMP = dump
    _T.clear()
    _T.update(targets)


def looks_like_page1(dec):
    """`dec` 是 page 1 第一个密文块解密后的 16 字节（至少前 8 字节有意义）。

    判据跳过第 4 字节（SQLite 头的「每页保留字节数」）：2.x 上它是 0x30(=48)，
    但别的版本未必等于密文侧的 reserve，硬校验会误杀正确密钥。
    """
    return (dec[0:2] == b'\x10\x00' and dec[2] == 1 and dec[3] == 1
            and dec[5] == 64 and dec[6] == 32 and dec[7] == 32)


def worker(args):
    start, end = args
    if not _T or not _DUMP:
        raise RuntimeError('worker received no targets/dump')
    lib = get_lib()
    hits = []
    with open(_DUMP, 'rb') as f:
        f.seek(start)
        buf = f.read(end - start + 32)
    n = len(buf) - 32
    # D(C1) 必须形如 (期望的明文头) xor IV，即前两字节 0x10^iv[0]、0x00^iv[1]。
    # 先用这两个字节常量筛掉 ~65535/65536 的候选，再做完整的 16 字节 XOR。
    items = [(name, iv, ct, bytes((iv[0] ^ 0x10, iv[1]))) for name, (iv, ct) in _T.items()]
    setkey = lib.AES_set_decrypt_key
    dec1 = lib.AES_decrypt
    ks, out = _ks, _out
    for i in range(0, n, 4):
        cand = buf[i:i + 32]
        setkey(cand, 256, ks)
        for name, iv, ct, want2 in items:
            dec1(ct, out, ks)
            raw = out.raw
            if raw[0:2] == want2:
                # CBC: P1 = D(C1) xor IV
                dec = bytes(a ^ b for a, b in zip(raw, iv))
                if looks_like_page1(dec):
                    hits.append((start + i, name, cand.hex()))
    return hits


def build_targets(sources):
    """sources: {tag: db_path} -> {tag: (iv, ct)}；顺便打印每个目标的 salt 与大小。"""
    t = {}
    for tag, p in sorted(sources.items()):
        if not os.path.exists(p):
            print('  missing: %s' % p)
            continue
        with open(p, 'rb') as f:
            b = f.read()
        if len(b) < 8192:
            print('  too small: %s' % p)
            continue
        io = PAGE - RESERVE
        t[tag] = (b[io:io + 16], bytes(b[16:32]))
        print('  target %-12s salt=%s size=%d' % (tag, b[:16].hex(), len(b)))
    return t


def parse_targets(specs):
    out = {}
    for s in specs:
        if '=' not in s:
            raise SystemExit('--target 需要写成 TAG=PATH（收到 %r）' % s)
        tag, path = s.split('=', 1)
        if not tag or not path:
            raise SystemExit('--target 需要写成 TAG=PATH（收到 %r）' % s)
        if tag in out:
            raise SystemExit('--target 的 tag 重复：%s' % tag)
        out[tag] = path
    return out


def selftest():
    """自足自检：不需要真实库。把一把合成的派生密钥注入临时转储，断言能扫到它、
    且偏移与密钥都逐字节一致。"""
    lib = try_get_lib()
    if lib is None:
        return SKIP
    iv = bytes(random.getrandbits(8) for _ in range(16))
    # 期望的 page 1 明文头：页大小 4096 | 读写版本 1 | 保留 48 | 载荷比 64/32/32
    plain = bytes.fromhex('1000010130402020') + bytes(random.getrandbits(8) for _ in range(8))
    key = bytes(random.getrandbits(8) for _ in range(32))
    # SQLCipher CBC: C1 = E(P1 xor IV)  ->  D(C1) xor IV == P1
    ks = ctypes.create_string_buffer(512)
    out = ctypes.create_string_buffer(16)
    lib.AES_set_encrypt_key(key, 256, ks)
    lib.AES_encrypt(bytes(a ^ b for a, b in zip(plain, iv)), out, ks)
    targets = {'selftest': (iv, out.raw)}

    blob = bytearray(os.urandom(3 << 20))
    at = 0x123456 & ~3
    blob[at:at + 32] = key
    tmp = os.path.join(tempfile.gettempdir(), 'selftest_dump.bin')
    with open(tmp, 'wb') as f:
        f.write(bytes(blob))
    print('selftest: injected key at 0x%X of %s' % (at, tmp))

    found = []
    with mp.Pool(max(1, os.cpu_count() or 4), initializer=init,
                 initargs=(tmp, targets)) as pool:
        for hits in pool.imap_unordered(worker, [(0, len(blob))]):
            found += hits
    ok = any(h[0] == at and h[2] == key.hex() for h in found)
    print('selftest hits: %d 处' % len(found))
    try:
        os.remove(tmp)
    except OSError:
        pass
    print('SELFTEST %s' % ('PASSED' if ok else 'FAILED'))
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog='sweep.py',
        description='从进程内存转储里扫出 2.x SQLCipher 库的派生密钥（只读转储与库）。',
        epilog='扫出的是候选派生密钥，请结合你自己的数据自行核对。',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dump', metavar='FILE',
                   default=os.environ.get('DUMP', os.path.join(HERE, 'dump', 'mem_<pid>.bin')),
                   help='内存转储文件（默认 ./dump/mem_<pid>.bin；环境变量 DUMP）')
    p.add_argument('--db-dir', metavar='DIR',
                   default=os.environ.get('WX_MSG_DIR', ''),
                   help='目标库所在目录：自动挂上 2.x 的 8 个已知库名'
                        '（环境变量 WX_MSG_DIR）')
    p.add_argument('--target', action='append', default=[], metavar='TAG=PATH',
                   help='显式指定一个目标库，可重复。TAG 只用于输出里区分命中')
    p.add_argument('--chunk-mb', type=int, default=CHUNK >> 20,
                   help='每个并行任务处理的块大小 MB（默认 %(default)s）')
    p.add_argument('--jobs', type=int, default=0,
                   help='并行进程数（默认 CPU 数）')
    p.add_argument('--selftest', action='store_true', help='跑自检后退出（不需要真实库）')
    return p


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    if not os.path.exists(args.dump):
        raise SystemExit('转储文件不存在：%s（用 --dump 指定，或先跑 dump_mem.py）'
                         % args.dump)
    sources = parse_targets(args.target)
    if args.db_dir:
        for n in DB_NAMES:
            tag = os.path.splitext(n)[0].lower()
            sources.setdefault(tag, os.path.join(args.db_dir, n))
    if not sources:
        raise SystemExit('没有目标库：用 --db-dir <DIR> 或 --target TAG=PATH 指定至少一个')

    size = os.path.getsize(args.dump)
    print('dump: %s (%d bytes)' % (args.dump, size))
    targets = build_targets(sources)
    if not targets:
        print('没有任何可用目标（都不存在或都太小）')
        return 1

    chunk = max(1, args.chunk_mb) << 20
    jobs = args.jobs or max(1, os.cpu_count() or 4)
    ranges = [(o, min(o + chunk, size)) for o in range(0, size, chunk)]
    print('\ntargets=%d  candidates=%d  chunks=%d  cpus=%d'
          % (len(targets), len(ranges) * (chunk // 4), len(ranges), jobs))

    t = time.time()
    found = []
    done = 0
    with mp.Pool(jobs, initializer=init, initargs=(args.dump, targets)) as pool:
        for hits in pool.imap_unordered(worker, ranges, chunksize=1):
            done += 1
            for h in hits:
                found.append(h)
                print('\n*** 命中候选：off=0x%08X target=%s key=%s' % h, flush=True)
            if done % 10 == 0 or hits:
                print('  ... %d/%d chunks (%.0fs)' % (done, len(ranges), time.time() - t),
                      flush=True)
    print('\n===== SWEEP DONE (%.1fs) =====' % (time.time() - t))
    if not found:
        print('没有命中。注意：**扫不到不等于不存在** —— '
              '目标页可能已被换出、被压缩，或不在本转储的范围内。')
    for f in found:
        print(f)
    return 0


if __name__ == '__main__':
    sys.exit(main())
