#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 4.x 的库里抽出「附件引用集」——用来判定磁盘上的附件是不是**孤立文件**。

背景（详见 `docs/detail/attachments.md`）：
  4.x 磁盘上的附件按 3 类存放，**每一类都有库里对应的标识**：

  | 磁盘位置 | 文件名是什么 | 库里的标识 |
  |---|---|---|
  | `msg/attach/<会话md5>/<YYYY-MM>/Img/<md5>[_t\\|_h\\|_W].dat` | **图片 id**（不是内容 md5） | `Msg_*.packed_info_data` 里的 32hex |
  | `msg/video/<YYYY-MM>/<md5>.mp4`（+ `.jpg` 封面） | **视频 id** | `Msg_*.packed_info_data`（base 43）里的 32hex |
  | `msg/file/<YYYY-MM>/<名字>` | 原始文件名，**或** `<内容md5>.<ext>` | `message_resource.db` 的 packed_info（原始名/本地名）<br>+ appmsg XML 的 `<title>` / `<md5>` |
  | `msg/migrate/File/<YYYY-MM>/<名字>` | 原始文件名 | 同上 |

  所以本工具产出两个集合：
    * `ids`   —— 32 位小写 hex（图片/视频/文件内容 md5 混在一起，够用）
    * `names` —— 文件名（原始名、本地名、`<title>`）

用法：
    wx4_attach_refs.py --root <xwechat_files> --out refs.json.gz
    wx4_attach_refs.py --root ... --out ... --work /tmp/wx4audit --no-cache

纪律：
  * **只读**源库；解密出的明文只落 `--work`（默认 /tmp）。
  * 打印的统计里不带任何会话名/文件名。
"""
import argparse
import ctypes
import ctypes.util
import gzip
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wx4_decrypt as D                                            # noqa: E402

HEX32 = re.compile(rb"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")
TAG = re.compile(r"<(?P<k>title|md5|emoticonmd5|filename|fileext|cdnattachurl|attachid)>(?P<v>[^<]*)</")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _local(p):
    """在 Windows 上把 /mnt/c/... 转成 C:\\...；在 Linux 上原样返回。

    本工具**跑在 Linux 侧**（要 ctypes 调 libzstd），所以默认不该动路径；
    但同一份代码在 Windows 的 python.exe 下也能跑，那里必须转（AGENTS.md §3）。
    """
    return D.to_win_path(p) if sys.platform == "win32" else p


# ---------------------------------------------------------------- zstd (ctypes)
class Zstd(object):
    """微信 4.x 的 message_content 用 zstd 压缩（WCDB_CT_message_content = 4）。

    Linux 侧的 python3 既没有 `pip` 也没有 `zstandard`，但系统里**有** `libzstd.so.1`
    ⇒ 直接 ctypes 调。实测确认可用，解出来的就是 appmsg 的 XML。
    """
    def __init__(self):
        lib = ctypes.util.find_library("zstd") or "libzstd.so.1"
        self.lib = ctypes.CDLL(lib)
        L = self.lib
        L.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        L.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        L.ZSTD_decompress.restype = ctypes.c_size_t
        L.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_size_t]
        L.ZSTD_isError.restype = ctypes.c_uint
        L.ZSTD_isError.argtypes = [ctypes.c_size_t]
        L.ZSTD_getErrorName.restype = ctypes.c_char_p
        L.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]

    def __call__(self, data):
        lib = self.lib
        src = ctypes.c_char_p(bytes(data))
        n = lib.ZSTD_getFrameContentSize(src, len(data))
        if n in (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFE) or n > (1 << 32):
            n = max(len(data) * 64, 1 << 20)
        dst = ctypes.create_string_buffer(int(n) + 1024)
        r = lib.ZSTD_decompress(dst, len(dst), src, len(data))
        if lib.ZSTD_isError(r):
            raise RuntimeError(lib.ZSTD_getErrorName(r).decode())
        return dst.raw[:r]


# ------------------------------------------------------------------- protobuf
def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def pb_fields(b):
    """极简 protobuf 解码，返回 [(field_no, value)]；解析不动就停。"""
    out = []
    i = 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            f, wt = key >> 3, key & 7
            if wt == 0:
                v, i = _varint(b, i)
                out.append((f, v))
            elif wt == 2:
                ln, i = _varint(b, i)
                out.append((f, b[i:i + ln]))
                i += ln
            elif wt == 5:
                out.append((f, b[i:i + 4]))
                i += 4
            elif wt == 1:
                out.append((f, b[i:i + 8]))
                i += 8
            else:
                break
    except (IndexError, ValueError):
        pass
    return out


# ------------------------------------------------------------------ 各库的抽取
def refs_from_message_db(dbpath, zstd, ids, names, stat):
    c = sqlite3.connect("file:%s?mode=ro" % dbpath.replace("?", "%3f"), uri=True)
    tabs = [r[0] for r in c.execute(
        "select name from sqlite_master where type='table' and name like 'Msg_%'")]
    stat["msg_tables"] += len(tabs)
    for t in tabs:
        cols = "local_type, WCDB_CT_message_content, message_content, compress_content, source, packed_info_data"
        for lt, ctc, mc, cc, src, pid in c.execute('select %s from "%s"' % (cols, t)):
            stat["msg_rows"] += 1
            base = (lt or 0) & 0xFFFFFFFF
            stat["type_%d" % base] = stat.get("type_%d" % base, 0) + 1
            # 1) packed_info_data / source：结构化，里面的 32hex 直接收
            for blob in (pid, src if isinstance(src, bytes) else None):
                if blob:
                    for m in HEX32.findall(bytes(blob)):
                        ids.add(m.decode())
            # 2) message_content / compress_content：可能是 zstd，也可能是明文 XML
            for raw in (mc, cc):
                if raw is None:
                    continue
                if isinstance(raw, str):
                    text, stat["plain_str"] = raw, stat["plain_str"] + 1
                else:
                    raw = bytes(raw)
                    if raw[:4] == ZSTD_MAGIC or ctc == 4:
                        try:
                            raw = zstd(raw)
                            stat["zstd_ok"] += 1
                        except Exception:
                            stat["zstd_fail"] += 1
                            continue
                    text = raw.decode("utf-8", "replace")
                if "<" not in text:
                    continue
                stat["xml_rows"] += 1
                for m in TAG.finditer(text):
                    k, v = m.group("k"), m.group("v").strip()
                    if not v:
                        continue
                    if k in ("title", "filename"):
                        names.add(v)
                    elif k in ("md5", "emoticonmd5") and len(v) == 32:
                        ids.add(v.lower())
                    elif k in ("cdnattachurl", "attachid"):
                        for h in HEX32.findall(v.encode()):
                            ids.add(h.decode())
    c.close()


def refs_from_resource_db(dbpath, ids, names, stat):
    c = sqlite3.connect("file:%s?mode=ro" % dbpath.replace("?", "%3f"), uri=True)
    for (di,) in c.execute("select data_index from MessageResourceDetail"):
        if not di:
            continue
        stat["data_index"] += 1
        if not di.isdigit():
            names.add(di)
    for (pid,) in c.execute("select packed_info from MessageResourceDetail where packed_info is not null"):
        b = bytes(pid)
        for f, v in pb_fields(b):
            if f == 1 and isinstance(v, (bytes, bytearray)):
                for g, w in pb_fields(bytes(v)):
                    if isinstance(w, (bytes, bytearray)) and w:
                        try:
                            names.add(w.decode("utf-8"))
                            stat["res_names"] += 1
                        except UnicodeDecodeError:
                            pass
        for m in HEX32.findall(b):
            ids.add(m.decode())
    for (pid,) in c.execute("select packed_info from MessageResourceInfo where packed_info is not null"):
        for m in HEX32.findall(bytes(pid)):
            ids.add(m.decode())
    c.close()


def refs_from_hardlink_db(dbpath, ids, names, stat):
    c = sqlite3.connect("file:%s?mode=ro" % dbpath.replace("?", "%3f"), uri=True)
    for t in ("file_hardlink_info_v4", "image_hardlink_info_v4", "video_hardlink_info_v4"):
        try:
            rows = c.execute('select md5, file_name from "%s"' % t).fetchall()
        except sqlite3.OperationalError:
            continue
        for md5, fn in rows:
            stat["hardlink"] += 1
            if md5:
                ids.add(md5.lower())
                stat["hardlink_md5"] += 1
            if fn:
                names.add(fn)
                stem = re.split(r"[._]", fn, maxsplit=1)[0]
                if re.fullmatch(r"[0-9a-f]{32}", stem):
                    ids.add(stem)
    c.close()


# ------------------------------------------------------------------------ main
def refs_from_generic_db(dbpath, ids, names, stat):
    """兜底扫描：`db_storage/` 下**所有**其它库的每一张表的每一个文本/二进制列。

    只做两件事：
      * 收集 32 位 hex 串（图片/表情/收藏/朋友圈都是内容寻址，靠这个就能对上）；
      * 收集看起来像「路径」的字符串里的**文件名部分**（含 `/` 或 `\\` 的长串）。
    不做列名判断 —— 多收集只会让「孤立」判得更保守，方向是安全的。
    """
    c = sqlite3.connect("file:%s?mode=ro" % dbpath.replace("?", "%3f"), uri=True)
    tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
    for t in tabs:
        try:
            cur = c.execute('select * from "%s"' % t)
        except sqlite3.Error:
            continue
        for row in cur:
            for v in row:
                if v is None:
                    continue
                if isinstance(v, str):
                    b = v.encode("utf-8", "replace")
                elif isinstance(v, (bytes, bytearray)):
                    b = bytes(v)
                else:
                    continue
                stat["generic_cells"] += 1
                for m in HEX32.findall(b):
                    ids.add(m.decode())
                if len(b) < 512 and (b"/" in b or b"\\" in b):
                    tail = re.split(rb"[/\\]", b)[-1]
                    if 0 < len(tail) < 200:
                        try:
                            names.add(tail.decode("utf-8"))
                        except UnicodeDecodeError:
                            pass
    c.close()


def find_dbs(acct):
    """返回 [(kind, path)]，kind ∈ message / resource / hardlink / other。"""
    out = []
    for sub, kind, pats in (("message", "message", ("message_",)),
                            ("message", "resource", ("message_resource.db",)),
                            ("hardlink", "hardlink", ("hardlink.db",))):
        d = os.path.join(acct, "db_storage", sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".db"):
                continue
            if kind == "message":
                if not re.fullmatch(r"message_\d+\.db", fn):
                    continue
            elif fn != pats[0]:
                continue
            out.append((kind, os.path.join(d, fn)))
    # 其余所有 .db（emoticon / favorite / sns / general / head_image / biz_* …）
    root = os.path.join(acct, "db_storage")
    known = set(os.path.abspath(p) for _, p in out)
    for dp, dns, fns in os.walk(root):
        for fn in sorted(fns):
            if not fn.endswith(".db"):
                continue
            p = os.path.join(dp, fn)
            if os.path.abspath(p) in known:
                continue
            out.append(("other", p))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="xwechat_files 目录（或单个账号目录）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default="/tmp/wx4audit/db")
    ap.add_argument("--keys", required=True,
                    help="salt/key 文本文件（格式见 wx4_decrypt.py 的 docstring）；本工具会自己解密库")
    ap.add_argument("--no-cache", action="store_true", help="不复用已解密好的明文库")
    a = ap.parse_args()

    root = _local(a.root)
    keys = D.load_keys(_local(a.keys))
    os.makedirs(a.work, exist_ok=True)
    zstd = Zstd()

    accts = []
    if os.path.isdir(os.path.join(root, "db_storage")):
        accts = [("", root)]
        root_parent = os.path.dirname(root)
    else:
        for e in sorted(os.scandir(root), key=lambda x: x.name):
            if e.is_dir() and os.path.isdir(os.path.join(e.path, "db_storage")):
                accts.append((e.name, e.path))
        root_parent = root

    import collections
    ids, names = set(), set()
    stat = collections.Counter()
    stat["accts"] = len(accts)
    read_accounts = set()
    t0 = time.time()
    for name, acct in accts:
        for kind, db in find_dbs(acct):
            rel = os.path.relpath(db, root_parent).replace("\\", "/")
            plain = os.path.join(a.work, rel.replace("/", "__") + ".plain")
            if a.no_cache or not os.path.exists(plain) or os.path.getmtime(plain) < os.path.getmtime(db):
                try:
                    blob = open(db, "rb").read()
                except OSError as e:
                    print("  跳过 %s: %s" % (rel, e))
                    continue
                salt = blob[:16].hex()
                if salt not in keys:
                    print("  跳过 %s: KEYS 里没有 salt=%s" % (rel, salt))
                    stat["no_key"] = stat.get("no_key", 0) + 1
                    continue
                if not D.page1_ok(keys[salt], blob[:D.PAGE]):
                    print("  跳过 %s: page-1 HMAC 不通过" % rel)
                    stat["bad_hmac"] = stat.get("bad_hmac", 0) + 1
                    continue
                open(plain, "wb").write(D.decrypt(blob, keys[salt]))
            try:
                if kind == "message":
                    refs_from_message_db(plain, zstd, ids, names, stat)
                elif kind == "resource":
                    refs_from_resource_db(plain, ids, names, stat)
                elif kind == "hardlink":
                    refs_from_hardlink_db(plain, ids, names, stat)
                else:
                    refs_from_generic_db(plain, ids, names, stat)
            except sqlite3.DatabaseError as e:
                print("  抽取失败 %s: %s" % (rel, e))
                stat["db_fail"] = stat.get("db_fail", 0) + 1
            stat["dbs"] = stat.get("dbs", 0) + 1
            read_accounts.add(name)
            print("  [%s] %-46s ids=%d names=%d" % (name or "-", rel, len(ids), len(names)), flush=True)

    out = {
        "meta": {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "root": os.path.basename(os.path.normpath(root)),
            "stats": dict(stat),
            "accounts_with_refs": sorted(read_accounts),
            "seconds": round(time.time() - t0, 1),
        },
        "ids": sorted(ids),
        "names": sorted(names),
    }
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("\nids=%d names=%d -> %s" % (len(ids), len(names), a.out))
    print("stats:", json.dumps(stat, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
