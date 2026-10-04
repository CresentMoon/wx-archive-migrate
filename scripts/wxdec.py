#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**你自己的**派生密钥解密微信 2.x / 3.x 的 SQLCipher 库（只读源文件）。

页布局（2.0.0.37 实测确认）：
    page_size = 4096, reserve = 48
    IV         = page[4048:4064]
    page 1 的密文 = page[16:4048]（前 16 字节是 salt），其余页 = page[0:4048]
    page 1 明文前 16 字节回填 "SQLite format 3\\0"

3.9.12.56 的 reserve 是 80，用 `--reserve 80`（其它版本请自行确认）。

本脚本**不负责取得密钥**，也不去猜：密钥必须由你提供。
需要往返校验 / 回写 / 3.x 集群级操作时，用 `wxdec3x.py`。

用法：
    python3 wxdec.py --key-env WX_KEY_HEX <src.db> <dst.db>
    python3 wxdec.py --key-hex <AES_KEY> --reserve 80 <src.db> <dst.db>
    python3 wxdec.py --selftest

退出码：0 = 成功 / 自检通过；1 = 坏密钥或尺寸异常；2 = 参数错误；77 = 自检跳过（缺依赖）。
"""
import argparse
import os
import sys
import time

PAGE, RESERVE = 4096, 48
MAGIC = b"SQLite format 3\x00"
SKIP = 77

# page 1 解密后（密文从 page[16] 起）前 8 字节就是 SQLite 头的偏移 16..23：
#   16..17 页大小（4096 的大端）  18 写版本  19 读版本
#   20    每页保留字节数          21 最大内嵌载荷比  22 最小  23 叶子
# 下面这两个签名就是 `sweep.py` 扫密钥时**在真实库上逐条命中**的那套判据。
# 注意它**刻意不检第 20 字节**：2.x/3.x 这个字段记的是不是就等于密文侧的 reserve
# （48 / 80），我们没有实测依据（未验证），硬校验会误杀正确密钥。
PAGE1_SIG = b'\x10\x00\x01\x01'
PAGE1_FRACTIONS = bytes((64, 32, 32))


def looks_like_page1(dec):
    """`dec` 至少 8 字节：判断它是不是 page 1 解密后的 SQLite 头。"""
    return dec[0:4] == PAGE1_SIG and dec[5:8] == PAGE1_FRACTIONS


def decrypt_file(key_hex, src, dst, page=PAGE, reserve=RESERVE):
    """把 `src` 解密到 `dst`，返回页数。坏密钥抛 SystemExit。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise SystemExit('密钥需要 32 字节（64 位 hex），实际 %d 字节' % len(key))
    ct_end = page - reserve
    data = open(src, 'rb').read()
    if len(data) % page:
        raise SystemExit('%s: 大小 %d 不是页大小 %d 的整数倍' % (src, len(data), page))
    n = len(data) // page
    out = bytearray(len(data))
    for p in range(n):
        base = p * page
        pg = data[base:base + page]
        start = 16 if p == 0 else 0
        iv = pg[ct_end:ct_end + 16]
        pt = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(pg[start:ct_end])
        out[base + start:base + ct_end] = pt
        if p == 0:
            out[base:base + 16] = MAGIC  # 用 SQLite 魔数顶掉 salt
    # 真正的「坏密钥」判据。**不能**用 out[:16] == MAGIC 来判 —— 它刚被我们自己写进去，
    # 那种检查永远为真（这一版之前的实现就是如此，错密钥会静默产出垃圾明文并报 OK）。
    if not looks_like_page1(out[16:24]):
        raise SystemExit('%s: 坏密钥（page 1 的 SQLite 头签名不匹配）' % src)
    if out[20] != (reserve & 0xFF):
        sys.stderr.write(
            '注意: page 1 头部记录的「每页保留字节数」是 %d，与 --reserve %d 不同。\n'
            '      这一字段与密文侧 reserve 的对应关系尚未验证；若解密结果正常，忽略即可。\n'
            % (out[20], reserve))
    with open(dst, 'wb') as f:
        f.write(bytes(out))
    return n


def selftest(page=PAGE, reserve=RESERVE):
    """自造一个两页的伪库：验证「加密 → 解密」原样还原，且坏密钥会被拒绝。

    保留区（每页末尾 reserve 字节）在明文里是 0、解密后也保持 0，
    所以两侧逐字节相等是**可以断言**的。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        print('SKIP: 自检需要 cryptography（pip install cryptography）')
        return SKIP
    import random
    import tempfile

    ct_end = page - reserve
    key = bytes(random.getrandbits(8) for _ in range(32))
    salt = bytes(random.getrandbits(8) for _ in range(16))
    plain = bytearray(page * 2)
    for p in range(2):
        base = p * page
        start = 16 if p == 0 else 0
        for i in range(base + start, base + ct_end):
            plain[i] = random.getrandbits(8)
    # page 1 的头：SQLite 魔数 + 偏移 16..23 的签名（否则自检会被自己的判据拒掉）
    plain[0:16] = MAGIC
    plain[16:20] = PAGE1_SIG
    plain[20] = reserve & 0xFF
    plain[21:24] = PAGE1_FRACTIONS

    blob = bytearray(page * 2)
    for p in range(2):
        base = p * page
        start = 16 if p == 0 else 0
        iv = bytes(random.getrandbits(8) for _ in range(16))
        blob[base + ct_end:base + ct_end + 16] = iv
        ct = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(
            bytes(plain[base + start:base + ct_end]))
        blob[base + start:base + ct_end] = ct
    blob[0:16] = salt  # 密文里 page 1 的头 16 字节是 salt，不是魔数

    with tempfile.TemporaryDirectory() as d:
        enc = os.path.join(d, 'enc.db')
        with open(enc, 'wb') as f:
            f.write(bytes(blob))
        dec = os.path.join(d, 'dec.db')
        n = decrypt_file(key.hex(), enc, dec, page, reserve)
        with open(dec, 'rb') as f:
            got = f.read()
        if got != bytes(plain):
            print('SELFTEST FAILED: 往返结果与原文不一致')
            return 1
        if n != 2:
            print('SELFTEST FAILED: 页数 %d != 2' % n)
            return 1
        try:
            decrypt_file(bytes(32).hex(), enc, os.path.join(d, 'bad.db'), page, reserve)
        except SystemExit:
            print('SELFTEST PASSED（往返一致，坏密钥被拒）')
            return 0
        print('SELFTEST FAILED: 坏密钥没有被拒绝')
        return 1


def build_parser():
    p = argparse.ArgumentParser(
        prog='wxdec.py',
        description='用你自己的派生密钥解密微信 2.x / 3.x 的 SQLCipher 库（只读源文件）。',
        epilog='密钥必须由你自行提供；请只用于你自己的数据。',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('src', nargs='?', metavar='<src.db>', help='加密的源库')
    p.add_argument('dst', nargs='?', metavar='<dst.db>', help='明文输出路径（会被覆盖）')
    g = p.add_argument_group('密钥')
    g.add_argument('--key-hex', metavar='<AES_KEY>',
                   help='32 字节 AES 派生密钥的十六进制（64 位 hex）')
    g.add_argument('--key-env', metavar='VAR',
                   help='从环境变量读取同一串十六进制（推荐：不会留在 shell 历史里）')
    g = p.add_argument_group('布局')
    g.add_argument('--page', type=int, default=PAGE, help='页大小（默认 %d）' % PAGE)
    g.add_argument('--reserve', type=int, default=RESERVE,
                   help='每页保留字节数（2.x 默认 %d；3.9 用 80）' % RESERVE)
    g = p.add_argument_group('其它')
    g.add_argument('--selftest', action='store_true', help='跑自检后退出（不需要真实库）')
    return p


def resolve_key(args):
    if args.key_env:
        val = os.environ.get(args.key_env)
        if not val:
            raise SystemExit('环境变量 %s 为空或不存在' % args.key_env)
        return val
    if args.key_hex:
        return args.key_hex
    raise SystemExit('必须提供 --key-env <VAR> 或 --key-hex <AES_KEY>（32 字节派生密钥）')


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args.page, args.reserve)
    if not args.src or not args.dst:
        ap.error('需要 <src.db> 与 <dst.db>（或改用 --selftest）')
    if os.path.abspath(args.src) == os.path.abspath(args.dst):
        raise SystemExit('源与目标是同一个文件：请另选输出路径（本脚本不做原地改写）')
    key_hex = resolve_key(args)
    t = time.time()
    n = decrypt_file(key_hex, args.src, args.dst, args.page, args.reserve)
    print('OK  %-14s %6d 页  %.1fs  -> %s'
          % (os.path.basename(args.src), n, time.time() - t, args.dst))
    return 0


if __name__ == '__main__':
    sys.exit(main())
