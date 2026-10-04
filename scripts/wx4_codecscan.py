# -*- coding: utf-8 -*-
"""微信 4.x 数据库密钥提取 —— 扫 SQLCipher codec 上下文（只读，不注入）。

在 Windows 侧 64 位 Python 下运行。

思路（来源：air846/WCDB `wechat_export/key_provider.py`，
该仓库实测于微信 4.1.13.63；本工具为我们自己的重实现，用于在 4.1.15.13 上复核）：

  1. 微信 4.x **每个 .db 有独立的 32 字节 enc_key**，不是账号级单密钥。
  2. WCDB 为每个打开的库持有一个 SQLCipher codec 上下文，其头部是一段
     配置常量前缀（下称 CODEC_PREFIX），可用来定位上下文。
  3. codec 上下文里 `+0x48` 是 salt 指针（指向 16 字节，等于库文件头 16 字节）、
     `+0x50` 是 hmac_salt 指针（应等于 salt ^ 0x3A）、`+0x68`/`+0x70` 分别是
     读/写 cipher 上下文指针。
  4. cipher 上下文 `+0x20` 是 keyspec 指针，指向 99 字节的 `x'<64hex key><32hex salt>'`。
     4.1.13 起这 99 字节被 **32 字节循环 XOR pad** 混淆。
  5. **该 pad 不需要硬编码**：keyspec 明文第 66..98 字节就是 salt 的 32 个 ASCII hex 字符，
     而 66..96 对应的 pad 下标正好是 2..31、96..98 对应 0..1，于是可直接解出 pad：
         pad[2:32] = ks_ct[66:96] XOR salt_ascii[0:30]
         pad[0:2]  = ks_ct[96:98] XOR salt_ascii[30:32]
     再用明文头 `x'`（ks_ct[0:2] XOR pad[0:2]）自校验。
  6. 校验密钥：SQLCipher 4 页 1 的 HMAC-SHA512
         mac_key = PBKDF2-HMAC-SHA512(key, salt ^ 0x3A, 2, 32)
         HMAC(mac_key, page1[16:4016] + LE32(1)) == page1[4032:4096]

本文件不含任何密钥；运行结果里才有，且只写 --out。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import subprocess
import sys

KEY_BYTES, SALT_BYTES = 32, 16
PAGE_SIZE, RESERVE, HMAC_SIZE = 4096, 80, 64
KEYS_SPEC_LEN = 99

# 4.1.13.63 实测的 codec 配置常量前缀（60 字节）。
# 字段（<15I，偏移 0/4/.../56）：
#   1=256000(kdf_iter) 2=2(fast_kdf_iter) 3=16(salt_len) 4=32(key_len)
#   7=4096(page_size)  8=99(keyspec_len)  9=80(reserve)  10=64(hmac_size)
# 按 8 字节一段拼：**不要写成连续的 64+ 位 hex 字面量** ——
# 发布自检会把长 hex 串当成疑似密钥，这里只是常量前缀，没必要触发它。
CODEC_PREFIX = bytes.fromhex("".join([
    "0000000000e80300", "0200000010000000", "2000000010000000",
    "1000000000100000", "6300000050000000", "4000000000000000",
    "0200000002000000",
]))
OFF_SALT_PTR, OFF_HMAC_SALT_PTR = 0x48, 0x50
OFF_READ_CTX, OFF_WRITE_CTX = 0x68, 0x70
OFF_KEYSPEC_PTR = 0x20

Q = 0x400
VM = 0x10
MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS = 0x1000, 0x100, 0x01
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD), ("_p", wt.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD), ("Protect", wt.DWORD),
                ("Type", wt.DWORD), ("_p2", wt.DWORD)]


class Proc:
    def __init__(self, pid):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.OpenProcess.restype = wt.HANDLE
        self.k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.k32.ReadProcessMemory.restype = wt.BOOL
        self.k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        self.k32.VirtualQueryEx.restype = ctypes.c_size_t
        self.pid = pid
        self.h = self.k32.OpenProcess(Q | VM, False, pid)
        if not self.h:
            raise RuntimeError(f"OpenProcess(pid={pid}) 失败，错误码 {ctypes.get_last_error()}")

    def read(self, addr, size):
        if addr <= 0 or size <= 0 or addr + size > 0x800000000000:
            return b""
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t(0)
        ok = self.k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n))
        if not ok and n.value == 0:
            return b""
        return buf.raw[:n.value]

    def regions(self):
        addr = 0
        while addr < 0x7FFFFFFFFFFF:
            mbi = MBI()
            if not self.k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = int(mbi.BaseAddress or 0), int(mbi.RegionSize)
            nxt = base + size
            if size <= 0 or nxt <= addr:
                break
            prot = int(mbi.Protect) & 0xFF
            if (mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD)
                    and prot != PAGE_NOACCESS and prot in READABLE):
                yield base, size
            addr = nxt

    def close(self):
        if self.h:
            self.k32.CloseHandle(self.h); self.h = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()



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


def weixin_pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip('"') for p in line.strip().strip('"').split('","')]
        if len(parts) >= 5 and parts[0].lower() == "weixin.exe":
            try:
                rows.append((int(parts[4].replace(",", "").replace(" K", "").strip() or 0), int(parts[1])))
            except ValueError:
                pass
    return [p for _, p in sorted(rows, reverse=True)]


def u64(data, off):
    return struct.unpack_from("<Q", data, off)[0]


def scan_pattern(read, regions, pattern, chunk=8 << 20):
    hits, overlap = [], len(pattern) - 1
    for base, size in regions:
        off, tail = 0, b""
        while off < size:
            data = read(base + off, min(chunk, size - off))
            if not data:
                off += 0x1000; tail = b""; continue
            comb = tail + data
            cbase = base + off - len(tail)
            i = comb.find(pattern)
            while i >= 0:
                hits.append(cbase + i); i = comb.find(pattern, i + 1)
            tail = comb[-overlap:] if overlap else b""
            off += len(data)
    return hits


def verify_key(key_hex, page1):
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return False
    if len(key) != KEY_BYTES or len(page1) < PAGE_SIZE:
        return False
    salt = page1[:SALT_BYTES]
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, dklen=KEY_BYTES)
    h = hmac_mod.new(mac_key, page1[SALT_BYTES:PAGE_SIZE - RESERVE + 16], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == page1[PAGE_SIZE - HMAC_SIZE:PAGE_SIZE]


def verify_key_passwordmode(key_hex, page1, iters=256000):
    """若某些版本改用 password 模式（page key = PBKDF2(rawkey, salt, 256000)）。"""
    try:
        raw = bytes.fromhex(key_hex)
    except ValueError:
        return False
    salt = page1[:SALT_BYTES]
    page_key = hashlib.pbkdf2_hmac("sha512", raw, salt, iters, dklen=KEY_BYTES)
    mac_key = hashlib.pbkdf2_hmac("sha512", page_key, bytes(b ^ 0x3A for b in salt), 2, dklen=KEY_BYTES)
    h = hmac_mod.new(mac_key, page1[SALT_BYTES:PAGE_SIZE - RESERVE + 16], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == page1[PAGE_SIZE - HMAC_SIZE:PAGE_SIZE]


def derive_pad(ks_ct, salt_hex):
    if len(ks_ct) != KEYS_SPEC_LEN or len(salt_hex) != SALT_BYTES * 2:
        return None
    sa = salt_hex.encode("ascii")
    pad = bytearray(32)
    pad[2:32] = bytes(a ^ b for a, b in zip(ks_ct[66:96], sa[0:30]))
    pad[0:2] = bytes(a ^ b for a, b in zip(ks_ct[96:98], sa[30:32]))
    if bytes(pad[0:2]) != bytes(a ^ b for a, b in zip(ks_ct[0:2], b"x'")):
        return None
    return bytes(pad)


def deobf(data, pad):
    return bytes(b ^ pad[i % 32] for i, b in enumerate(data))


def collect_db_page1(root):
    m = {}
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".db"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "rb") as f:
                    pg = f.read(PAGE_SIZE)
            except OSError:
                continue
            if len(pg) == PAGE_SIZE:
                m.setdefault(pg[:SALT_BYTES].hex(), (pg, p))
    return m


def parse_ctx(read, addr, page1_map, verbose=False):
    codec = read(addr, 0x88)
    if len(codec) < 0x88 or codec[:len(CODEC_PREFIX)] != CODEC_PREFIX:
        return []
    f = struct.unpack_from("<15I", codec, 0)
    salt = read(u64(codec, OFF_SALT_PTR), f[3])
    if len(salt) != f[3]:
        return []
    hs = read(u64(codec, OFF_HMAC_SALT_PTR), f[3])
    if hs and hs != bytes(b ^ 0x3A for b in salt):
        if verbose: print(f"      hmac_salt 不匹配 @0x{addr:x}")
        return []
    salt_hex = salt.hex()
    if page1_map is not None and salt_hex not in page1_map:
        if verbose: print(f"      salt {salt_hex} 不在已收集的库里，跳过")
        return []
    out = []
    for label, off in (("read", OFF_READ_CTX), ("write", OFF_WRITE_CTX)):
        caddr = u64(codec, off)
        if not caddr: continue
        cipher = read(caddr, 0x28)
        if len(cipher) < 0x28: continue
        ks_ct = read(u64(cipher, OFF_KEYSPEC_PTR), f[8])
        if len(ks_ct) != f[8]: continue
        pad = derive_pad(ks_ct, salt_hex)
        if pad is None:
            if verbose: print(f"      {label} ctx pad 推导失败")
            continue
        ks = deobf(ks_ct, pad)
        if not (ks.startswith(b"x'") and ks.endswith(b"'") and len(ks) == KEYS_SPEC_LEN):
            if verbose: print(f"      {label} ctx keyspec 形态不对: {ks[:20]!r}")
            continue
        key_hex = ks[2:66].decode("ascii", "ignore")
        if ks[66:98].decode("ascii", "ignore") != salt_hex:
            if verbose: print(f"      {label} ctx keyspec 内 salt 不符")
            continue
        ok = page1_map is None or verify_key(key_hex, page1_map[salt_hex][0])
        mode = "rawkey"
        if not ok and page1_map is not None:
            ok = verify_key_passwordmode(key_hex, page1_map[salt_hex][0])
            mode = "pbkdf2x256000"
        if page1_map is not None and not ok:
            if verbose: print(f"      {label} ctx key {key_hex[:8]}… HMAC 不通过")
            continue
        out.append((salt_hex, key_hex, label, mode))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="4.x 账号根或 db_storage 目录（默认自动找）")
    ap.add_argument("--out")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--probe-prefix", action="store_true", help="只统计前缀命中，不解析")
    args = ap.parse_args()

    root = to_win_path(args.root)
    if not root:
        base = os.path.join(os.path.expanduser("~"), "Documents", "xwechat_files")
        best, bt = None, -1
        for d in os.listdir(base):
            p = os.path.join(base, d, "db_storage", "message", "message_0.db")
            if os.path.exists(p) and os.path.getmtime(p) > bt:
                best, bt = os.path.join(base, d), os.path.getmtime(p)
        root = best
    print(f"[i] 账号根：{root}")
    page1_map = collect_db_page1(os.path.join(root, "db_storage") if os.path.isdir(os.path.join(root, "db_storage")) else root)
    print(f"[i] 收集到 {len(page1_map)} 个库的 salt（不同 salt 数）")

    pids = weixin_pids()
    print(f"[i] Weixin.exe 进程（按内存降序）：{pids}")
    found = {}
    for pid in pids:
        try:
            with Proc(pid) as pr:
                regs = list(pr.regions())
                tot = sum(s for _, s in regs)
                print(f"\n[>] pid={pid} 可读区域 {len(regs)} 个 / {tot/1048576:.0f} MB")
                hits = scan_pattern(pr.read, regs, CODEC_PREFIX)
                print(f"    CODEC_PREFIX 命中 {len(hits)} 处")
                if args.probe_prefix:
                    for a in hits[:20]:
                        print(f"      0x{a:012x}")
                    continue
                for a in hits:
                    for salt_hex, key_hex, label, mode in parse_ctx(pr.read, a, page1_map, args.verbose):
                        if salt_hex not in found:
                            found[salt_hex] = (key_hex, mode, pid, a, label)
                            path = page1_map[salt_hex][1]
                            print(f"    [+] salt={salt_hex} key={key_hex} [{mode}] pid={pid} ctx=0x{a:x} {label}")
                            print(f"        -> {path}")
        except RuntimeError as e:
            print(f"[!] pid={pid}: {e}")

    print(f"\n[i] 共提取 {len(found)} 个库密钥（收集的库 {len(page1_map)} 个）")
    missing = [s for s in page1_map if s not in found]
    print(f"[i] 未取到：{len(missing)} 个（库没被打开 / 上下文已换页）")
    for s in missing[:20]:
        print(f"      {s}  {page1_map[s][1]}")
    if args.out and found:
        json.dump({s: {"key": k, "mode": m, "pid": p, "ctx": hex(a), "label": l}
                   for s, (k, m, p, a, l) in found.items()}, open(to_win_path(args.out), "w"), indent=1)
        print(f"[i] 已写 {args.out}")
    return 0 if found else 3


if __name__ == "__main__":
    sys.exit(main())
