#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在转储的**指定窗口**里找 2.x 的原始密钥 K：满足
`PBKDF2-HMAC-SHA1(K, salt, 64000, 32) == 已找到的那把派生密钥`。

为什么是「窗口」而不是整份转储：一个候选要跑 64000 轮 PBKDF2（约 16 ms），
整份 477 MB、步长 4 就是约 1.2 亿个候选 —— 单核要几十天。所以只扫
「派生密钥 / 它的缓存 / codec 结构体曾出现的位置」周围有限的范围。
窗口必须由你自己给出（本脚本不猜），例如先用 `sweep.py` 找到派生密钥命中的
偏移 `off`，再拿 `--window` 圈住它附近。

用法：
    python3 rawhunt_rawkey.py --dump ./dump/mem_<pid>.bin \\
        --salt-hex <salt> --derived-hex <derived> \\
        --window 0x0D5A0000-0x0D5C0000

    python3 rawhunt_rawkey.py --selftest        # 自造转储，端到端验一次

环境变量（对应参数未给出时作为兜底）：
    DUMP            内存转储（默认 ./dump/mem_<pid>.bin）
    WX_SALT_HEX     该库的 salt（16 字节 hex）
    WX_DERIVED_HEX  已知的派生密钥（32 字节 hex）

退出码：0 = 跑完（有没有命中看 stdout）；1 = 输入不合法；2 = 参数错误；77 = 自检跳过。
"""
import argparse
import hashlib
import multiprocessing as mp
import os
import random
import sys
import tempfile
import time

SKIP = 77
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DUMP = os.path.join(HERE, 'dump', 'mem_<pid>.bin')
STEP = 4
KDF_ITER = 64000
KDF_HASH = 'sha1'

_SALT = None
_DER = None
_DUMP = None
_STEP = STEP
_ITER = KDF_ITER


def init(dump, salt, der, step, iters):
    global _DUMP, _SALT, _DER, _STEP, _ITER
    _DUMP, _SALT, _DER, _STEP, _ITER = dump, salt, der, step, iters


def worker(rng):
    a, b = rng
    a = max(0, a)
    b = min(os.path.getsize(_DUMP), b)
    if b <= a:
        return []
    hits = []
    with open(_DUMP, 'rb') as f:
        f.seek(a)
        buf = f.read(b - a + 32)
    pbkdf2 = hashlib.pbkdf2_hmac
    for i in range(0, max(0, len(buf) - 32), _STEP):
        cand = buf[i:i + 32]
        if pbkdf2(KDF_HASH, cand, _SALT, _ITER, 32) == _DER:
            hits.append((a + i, cand.hex()))
    return hits


def parse_int(s):
    s = s.strip()
    return int(s, 16) if s.lower().startswith('0x') else int(s, 10)


def parse_window(s):
    """`START-END`（十进制或 0x 十六进制）。END 必须大于 START。"""
    if '-' not in s[1:]:
        raise SystemExit('--window 需要写成 START-END（收到 %r）' % s)
    i = s.index('-', 1)
    a, b = parse_int(s[:i]), parse_int(s[i + 1:])
    if b <= a:
        raise SystemExit('--window 的 END 必须大于 START（收到 %r）' % s)
    return a, b


def clip_windows(windows, size, step):
    out = []
    for a, b in windows:
        a, b = max(0, a), min(size, b)
        if b > a:
            out.append((a, b))
    return out


def selftest():
    """自造转储：把一把随机的原始密钥埋进去，验证能按 PBKDF2 关系把它捞回来。"""
    master = bytes(random.getrandbits(8) for _ in range(32))
    salt = bytes(random.getrandbits(8) for _ in range(16))
    der = hashlib.pbkdf2_hmac(KDF_HASH, master, salt, KDF_ITER, 32)
    size = 1 << 16
    blob = bytearray(os.urandom(size))
    at = (0x4000 & ~(STEP - 1)) + 4
    blob[at:at + 32] = master
    tmp = os.path.join(tempfile.gettempdir(), 'rawhunt_selftest.bin')
    with open(tmp, 'wb') as f:
        f.write(bytes(blob))

    # 窗口必须覆盖 at，但**不要**正好从 at 开始 —— 否则窗口起点对齐会掩盖偏移 bug。
    windows = [(at - 64, at + 96)]
    jobs = max(1, os.cpu_count() or 4)
    jobs = min(jobs, 4)
    found = []
    try:
        with mp.Pool(jobs, initializer=init,
                     initargs=(tmp, salt, der, STEP, KDF_ITER)) as pool:
            for h in pool.imap_unordered(worker, windows):
                found += h
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    ok = any(off == at and k == master.hex() for off, k in found)
    print('selftest: 期望 %d 处命中，实得 %d 处' % (1, len(found)))
    print('SELFTEST %s' % ('PASSED' if ok else 'FAILED'))
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog='rawhunt_rawkey.py',
        description='在转储的指定窗口里按 PBKDF2 关系反查 2.x 的原始密钥 K。',
        epilog='请只用于你自己的转储。',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--dump', metavar='FILE', default=os.environ.get('DUMP', DEFAULT_DUMP),
                   help='内存转储文件（默认 ./dump/mem_<pid>.bin；环境变量 DUMP）')
    p.add_argument('--salt-hex', metavar='HEX',
                   default=os.environ.get('WX_SALT_HEX', ''),
                   help='该库的 salt（16 字节 hex；环境变量 WX_SALT_HEX）')
    p.add_argument('--derived-hex', metavar='HEX',
                   default=os.environ.get('WX_DERIVED_HEX', ''),
                   help='已知的 32 字节派生密钥（环境变量 WX_DERIVED_HEX）')
    p.add_argument('--window', action='append', default=[], metavar='START-END',
                   help='要扫的字节区间，可重复。支持 0x 前缀。**必须显式给出**：'
                        '整份转储扫一遍是几十天的量级，本脚本拒绝猜')
    p.add_argument('--step', type=int, default=STEP,
                   help='扫描步长字节（默认 %(default)s）')
    p.add_argument('--kdf-iter', type=int, default=KDF_ITER,
                   help='PBKDF2 迭代次数（默认 %(default)s，与 2.x SQLCipher 一致）')
    p.add_argument('--jobs', type=int, default=0, help='并行进程数（默认 CPU 数）')
    p.add_argument('--selftest', action='store_true', help='跑自检后退出（不需要真实数据）')
    return p


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()

    if not args.window:
        ap.error('必须至少给一个 --window START-END（整份转储扫一遍是几十天的量级）')
    if not args.salt_hex or not args.derived_hex:
        ap.error('必须提供 --salt-hex 与 --derived-hex（或对应的环境变量）')
    if args.step < 1:
        ap.error('--step 必须 >= 1')
    if not os.path.exists(args.dump):
        raise SystemExit('转储文件不存在：%s（用 --dump 指定，或先跑 dump_mem.py）' % args.dump)

    try:
        salt = bytes.fromhex(args.salt_hex)
        der = bytes.fromhex(args.derived_hex)
    except ValueError as e:
        raise SystemExit('salt / derived 不是合法的十六进制：%s' % e)
    if len(salt) != 16:
        raise SystemExit('salt 需要 16 字节（32 位 hex），实际 %d 字节' % len(salt))
    if len(der) != 32:
        raise SystemExit('derived 需要 32 字节（64 位 hex），实际 %d 字节' % len(der))

    size = os.path.getsize(args.dump)
    windows = clip_windows([parse_window(s) for s in args.window], size, args.step)
    if not windows:
        raise SystemExit('所有 --window 都落在文件之外（文件只有 %d 字节）' % size)
    total = sum((b - a) // args.step for a, b in windows)
    jobs = args.jobs or max(1, os.cpu_count() or 4)
    print('dump %s (%d bytes)' % (args.dump, size))
    print('windows=%d candidates=%d  est %.0f s on %d cpus'
          % (len(windows), total, total * 0.016 / jobs, jobs))

    t = time.time()
    found = []
    with mp.Pool(jobs, initializer=init,
                 initargs=(args.dump, salt, der, args.step, args.kdf_iter)) as pool:
        for h in pool.imap_unordered(worker, windows):
            if h:
                found += h
                for off, k in h:
                    print('*** 命中：off=0x%08X key=%s ***' % (off, k), flush=True)
    print('done %.0fs  hits=%d' % (time.time() - t, len(found)))
    if not found:
        print('窗口内没有命中。可以先把窗口开大一些再试 —— '
              '但请注意候选数与耗时是线性关系。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
