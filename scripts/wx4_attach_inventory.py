#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 xwechat_files 里的每个文件算「两个哈希」，供附件去重/孤立审计用。

  * `md5_raw`  —— **落盘字节**的 md5（加密的就是密文的）
  * `md5_norm` —— **规范化内容**的 md5：
      - 已经是明文（JPEG/PNG/ZIP/PDF/…）→ 等于 md5_raw
      - 单字节 XOR 0xA0 容器（`.dat` 图片，实测确认）→ 解密后的 md5
      - `\\x07\\x08V1\\x08\\x07` / `\\x07\\x08V2\\x08\\x07` 容器 → **解不开**，退回 md5_raw，
        并打上 `container-v1` / `container-v2` 标签（格式未破解，见 `docs/detail/attachments.md` §6.1）
      - 其它 → 等于 md5_raw

所以「明文一份 + 加密一份」的同一张图会在 `md5_norm` 上合并。

**实测确认的规模事实**：在 Windows 原生 python.exe 下读这套目录，
大文件 ~730 MB/s、小文件 ~4200 个/秒；同一份数据从 WSL 的 `/mnt/c` 读只有 ~200 MB/s
（drvfs 逐文件 syscall 开销）⇒ **这个脚本应当用 Windows 侧的 python.exe 跑**。
为了两边都能用，路径在 Windows 上会自动 `/mnt/c/…` → `C:\\…`。

用法：
    python.exe wx4_attach_inventory.py --root "C:\\Users\\<你>\\Documents\\xwechat_files" \\
                                      --out  "C:\\_wx4_pathprobe\\attach-inventory.tsv"
    python3   wx4_attach_inventory.py --root /mnt/c/... --out /tmp/attach-inventory.tsv

输出：TSV，列为
    relpath <TAB> size <TAB> mtime <TAB> md5_raw <TAB> md5_norm <TAB> kind <TAB> nlink <TAB> inode

`nlink` / `inode` 是**必须的**：客户端自己已经对一部分重复文件做了硬链接
（实测确认：同一组的 6 个 `.pptx` 共享一个 inode、`st_nlink=6`），
所以「能回收多少空间」要按 **不同 inode 的个数**算，不是按文件个数算。

纪律：**只读**，不写、不改、不删任何被扫描的文件。
"""
import argparse
import hashlib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# 明文特征（尽量选得长一点，降低 XOR 误判概率）
MAGICS = (
    b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a",
    b"RIFF", b"BM", b"II*\x00", b"MM\x00*",
    b"%PDF", b"PK\x03\x04", b"PK\x05\x06", b"7z\xbc\xaf\x27\x1c",
    b"Rar!\x1a\x07", b"\x1f\x8b", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\x50\x4b\x03\x04",
    b"<?xml", b"{\\rtf", b"\x00\x00\x01\x00", b"\x00\x00\x02\x00",
    b"ID3", b"OggS", b"fLaC", b"\x25\x21\x50\x53",  # %!PS
)
C_V1 = b"\x07\x08V1\x08\x07"
C_V2 = b"\x07\x08V2\x08\x07"
XOR = 0xA0
_XLAT = bytes((i ^ XOR) for i in range(256))


def to_win_path(p):
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p or "")
    if m:
        return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\"))
    return p


def local(p):
    return to_win_path(p) if sys.platform == "win32" else p


def classify(head):
    if any(head.startswith(m) for m in MAGICS):
        return "plain"
    if head.startswith(C_V1):
        return "container-v1"
    if head.startswith(C_V2):
        return "container-v2"
    if any(head.translate(_XLAT).startswith(m) for m in MAGICS):
        return "xor-a0"
    return "raw"


def do_file(args):
    root, rel = args
    p = os.path.join(root, rel.replace("/", os.sep))
    try:
        st = os.stat(p)
        h1 = hashlib.md5()
        with open(p, "rb") as f:
            head = f.read(32)
            kind = classify(head)
            need_norm = kind == "xor-a0"
            h2 = hashlib.md5() if need_norm else None
            buf = head
            while buf:
                h1.update(buf)
                if need_norm:
                    h2.update(buf.translate(_XLAT))
                buf = f.read(1 << 20)
        if not need_norm:
            h2 = h1
        return (rel, st.st_size, int(st.st_mtime), h1.hexdigest(), h2.hexdigest(), kind,
                st.st_nlink, st.st_ino)
    except OSError as e:
        return (rel, -1, 0, "ERR", str(e).replace("\t", " "), "error", 0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="调试用：只处理前 N 个文件")
    a = ap.parse_args()

    root = local(a.root)
    out = local(a.out)
    t0 = time.time()
    rels = []
    for dp, dns, fns in os.walk(root):
        rel = os.path.relpath(dp, root)
        for fn in fns:
            rels.append(fn if rel == "." else rel.replace("\\", "/") + "/" + fn)
    rels.sort()
    if a.limit:
        rels = rels[:a.limit]
    print("文件数 %d，扫描根 %s，%.1fs" % (len(rels), root, time.time() - t0), flush=True)

    n = 0
    kinds = {}
    with open(out, "w", encoding="utf-8", newline="\n") as fo:
        fo.write("#relpath\tsize\tmtime\tmd5_raw\tmd5_norm\tkind\tnlink\tinode\n")
        with ThreadPoolExecutor(max_workers=a.threads) as ex:
            for r in ex.map(do_file, ((root, x) for x in rels), chunksize=64):
                fo.write("\t".join(str(x) for x in r) + "\n")
                kinds[r[5]] = kinds.get(r[5], 0) + 1
                n += 1
                if n % 20000 == 0:
                    print("  %d/%d  %.1fs" % (n, len(rels), time.time() - t0), flush=True)
    print("完成 %d 个文件，%.1fs -> %s" % (n, time.time() - t0, out))
    print("类型分布:", sorted(kinds.items(), key=lambda x: -x[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
