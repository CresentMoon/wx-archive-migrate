#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解密微信 4.x 的 SQLCipher 库（AES-256-CBC / page 4096 / reserve 80 / raw key）。

密钥来源：由 `wx4_codecscan.py` 提取（逐库 HMAC 校验过），或你自己已有的映射。

用法：
    wx4_decrypt.py <库路径> [-o 输出] --keys <密钥文件>
    wx4_decrypt.py <库路径> --check        # 只校验 page-1 HMAC，不解密

密钥文件格式（纯文本，一行一库；`salt` 就是该库文件头的前 16 字节）：
    salt=<32 位 hex> key=<64 位 hex>

纪律：
  * **只读源库**；输出默认写 /tmp，绝不覆盖源文件。
  * 明文落盘是**有意的**临时行为（要喂 sqlite3）；用完自己清。
  * 不打印密钥。
"""
import argparse
import hashlib
import hmac as hmac_mod
import os
import re
import struct
import sys

PAGE = 4096
RESERVE = 80
HMAC_SIZE = 64
KEY_BYTES = 32
SALT_BYTES = 16
MAGIC = b"SQLite format 3\x00"
IV_OFF = PAGE - RESERVE            # 4016
MAC_OFF = PAGE - HMAC_SIZE         # 4032
CT_END = PAGE - RESERVE + 16       # 4032

# 密钥文件**必须显式给出**：它不该有"默认位置"，更不该在仓库里。
DEFAULT_KEYS = None



def to_win_path(p):
    """把 WSL 的 /mnt/<盘>/... 换成 <盘>:\\...，供 Windows 侧 Python 使用。

    在 Windows Python 里拿到 Linux 路径会直接 ENOENT / PermissionError ——
    这是本项目反复踩到的坑（AGENTS.md §3）。
    """
    if not p:
        return p
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p)
    if m:
        return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\"))
    return p


def load_keys(path):
    """读 KEYS_4x.txt：形如  salt=<32hex> ... key=<64hex>  """
    keys = {}
    salt = None
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.search(r"\bsalt=([0-9a-fA-F]{32})", line)
        if m:
            salt = m.group(1).lower()
            continue
        m = re.search(r"\bkey=([0-9a-fA-F]{64})", line)
        if m and salt:
            keys[salt] = bytes.fromhex(m.group(1))
            salt = None
    if not keys:
        raise SystemExit("%s 里没解析到任何 salt/key" % path)
    return keys


def mac_key_of(key, salt):
    return hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, KEY_BYTES)


def page1_ok(key, page1):
    salt = page1[:SALT_BYTES]
    mk = mac_key_of(key, salt)
    h = hmac_mod.new(mk, page1[SALT_BYTES:CT_END], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == page1[MAC_OFF:PAGE]


def _make_dec(key):
    """返回 dec(ct, iv)。两个后端都支持：pycryptodome（Windows 侧有）或 cryptography（Linux 侧有）。"""
    try:
        from Crypto.Cipher import AES as _AES

        def dec(ct, iv):
            return _AES.new(key, _AES.MODE_CBC, iv).decrypt(ct)
        return dec
    except ImportError:
        pass
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def dec(ct, iv):
        return Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(ct)
    return dec


def decrypt(blob, key, wal=b""):
    """解密整库；wal 非空时把 WAL 帧一并合并（见 merge_wal）。"""
    dec = _make_dec(key)

    if len(blob) % PAGE:
        raise SystemExit("大小 %d 不是 %d 的整数倍" % (len(blob), PAGE))
    out = bytearray()
    for off in range(0, len(blob), PAGE):
        pg = blob[off:off + PAGE]
        iv = pg[IV_OFF:IV_OFF + 16]
        if off == 0:
            out += MAGIC + dec(pg[SALT_BYTES:IV_OFF], iv)
        else:
            out += dec(pg[:IV_OFF], iv)
        out += pg[IV_OFF:]          # 保留段原样回填，保持页对齐
    out[20] = RESERVE               # 页头 reserved 必须写回 80
    if wal:
        n = merge_wal(out, wal, dec)
        print("WAL      : 合并 %d 帧" % n)
    return bytes(out)


def merge_wal(out, wal, dec):
    """[实验性 · 未验证] 把 WAL 里的页覆盖到已解密的页镜像上。

    实测确认（4.1.15.13 / message_resource.db + hardlink.db）：
      * WAL 头部与帧头是**明文**（标准 SQLite 帧结构：pgno / nTruncate / salt / cksum）；
      * 帧内页数据的布局与主库页**完全一致**（IV 在 4016、保留段 80）；
      * **page 1 的帧要去掉开头 16 字节**，与主库一样密文从偏移 16 起。

    ⚠️ **但帧头的 salt 字段与 WAL 头部对不上**（同一 salt1 只差最低几个字节，
    且低字节按 -1 递减），标准 SQLite 的「salt 不匹配即停止」在这里不成立，
    ⇒ **无法用 salt 判定哪一代帧是当前的**。本函数改用经验规则：
    **WAL 是环形的，重启时头部就地重写、帧从偏移 32 重新覆盖，所以「第 0 帧所属的那一代」是最新的一代。**
    该规则在本机两种库上都给出 `integrity_check = ok`，但**没有独立证据**。

    实测结果：message_resource / hardlink 两库，**合并前后行数与最大时间戳完全一致**
    （主库本身已是 checkpoint 后的完整快照）⇒ **本功能目前没有实际收益**，
    保留是为了以后版本万一真的把数据滞留在 WAL 里。默认关闭。
    """
    if len(wal) < 32 + 24 + PAGE:
        return 0
    magic, ver, psz, ckpt = struct.unpack(">IIII", wal[:16])
    if magic not in (0x377f0682, 0x377f0683) or psz != PAGE:
        print("WAL      : 头部不像标准 SQLite WAL（magic=%08x psz=%d），跳过" % (magic, psz))
        return 0
    frames = []
    off = 32
    while off + 24 + PAGE <= len(wal):
        fr = wal[off:off + 24 + PAGE]
        pgno, dbsz = struct.unpack(">II", fr[:8])
        frames.append((pgno, dbsz, fr[8:16], fr[24:]))
        off += 24 + PAGE
    cur = frames[0][2]                       # 第 0 帧所属的一代 = 最新一代
    last = max((i for i, f in enumerate(frames) if f[2] == cur and f[1]), default=-1)
    if last < 0:
        print("WAL      : 第 0 帧所属那一代没有提交帧，跳过")
        return 0
    n = 0
    for pgno, dbsz, salt, page in frames[:last + 1]:
        if salt != cur:
            continue
        iv = page[IV_OFF:IV_OFF + 16]
        ct = page[SALT_BYTES:IV_OFF] if pgno == 1 else page[:IV_OFF]
        pt = dec(ct, iv) + page[IV_OFF:]
        if pgno == 1:
            out[:PAGE] = MAGIC + bytes(pt)
        else:
            start = (pgno - 1) * PAGE
            if start + PAGE > len(out):
                out.extend(b"\x00" * (start + PAGE - len(out)))
            out[start:start + PAGE] = pt
        n += 1
    print("WAL      : 共 %d 帧 -> 采用第 0 帧所属那一代 %d 帧（未验证规则）" % (len(frames), n))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("-o", "--out")
    ap.add_argument("--keys", required=True, help="salt/key 文本文件（格式见模块 docstring）")
    ap.add_argument("--check", action="store_true", help="只校验 page-1 HMAC")
    ap.add_argument("--wal", action="store_true",
                    help="[实验性·未验证] 合并 <库>-wal 的帧；本机实测与不合并结果相同")
    a = ap.parse_args()

    keys = load_keys(to_win_path(a.keys))
    blob = open(to_win_path(a.db), "rb").read()
    salt = blob[:SALT_BYTES].hex()
    if salt not in keys:
        raise SystemExit("KEYS 里没有 salt=%s（该库未打开过？）" % salt)
    key = keys[salt]

    ok = page1_ok(key, blob[:PAGE])
    print("salt     : %s" % salt)
    print("页数     : %d" % (len(blob) // PAGE))
    print("page1 HMAC: %s" % ("通过" if ok else "**不通过**"))
    if not ok:
        return 2
    if a.check:
        return 0

    out = a.out or ("/tmp/%s.plain.db" % os.path.basename(a.db))
    wal = b""
    if a.wal:
        walpath = to_win_path(a.db) + "-wal"
        if os.path.exists(walpath):
            wal = open(walpath, "rb").read()
        else:
            print("WAL      : 没有 %s" % walpath)
    plain = decrypt(blob, key, wal)
    if not plain.startswith(MAGIC):
        raise SystemExit("解密结果头部不是 SQLite magic，布局假设可能变了")
    open(out, "wb").write(plain)
    print("明文     : %s (%d 字节)" % (out, len(plain)))
    print("页头     : %s" % plain[:24].hex(" "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
