#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件审计：拿「磁盘清单 + 库引用集」判**重复**与**孤立**，出报告。

输入：
  * `--inv`  由 `wx4_attach_inventory.py` 产出的 TSV（Windows 侧跑得快）
  * `--refs` 由 `wx4_attach_refs.py` 产出的 refs.json.gz（Linux 侧跑）

「孤立」的判定口径（三条任一命中即算**有索引**）：
  1. **文件名**出现在库里的文件名集合（appmsg `<title>`、`message_resource.data_index`、
     `MessageResourceDetail.packed_info`、`hardlink.file_name`）；
  2. **文件名主干**（去掉 `_t`/`_h`/`_W`/扩展名）是 32 位 hex，且出现在库里的 id 集合；
  3. **内容 md5**（`md5_raw` 或 `md5_norm`）出现在库里的 id 集合。

用法：
    wx4_attach_audit.py --inv attach-inventory.tsv --refs refs.json.gz --out reports/attach-audit
"""
import argparse
import collections
import csv
import gzip
import json
import os
import re
import sys
import time

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX_IN_NAME = re.compile(r"[0-9a-f]{32}")
TRAIL_HEX = re.compile(r"[_-]?[0-9a-f]{32}$")

# 目录 -> (大类, 是否参与「孤立」判定)
AREAS = [
    (re.compile(r"^[^/]+/msg/attach/"), "msg/attach 图片附件", True),
    (re.compile(r"^[^/]+/msg/file/"), "msg/file 文件附件", True),
    (re.compile(r"^[^/]+/msg/video/"), "msg/video 视频附件", True),
    (re.compile(r"^[^/]+/msg/migrate/"), "msg/migrate 迁移暂存", True),
    (re.compile(r"^[^/]+/cache/"), "cache 会话缓存", True),
    (re.compile(r"^[^/]+/business/xweb/"), "business/xweb 内嵌浏览器缓存", False),
    (re.compile(r"^[^/]+/business/"), "business 业务数据", True),
    (re.compile(r"^[^/]+/resource/"), "resource 运行资源", False),
    (re.compile(r"^[^/]+/db_storage/"), "db_storage 数据库", False),
    (re.compile(r"^[^/]+/config/"), "config 配置", False),
    (re.compile(r"^[^/]+/temp/"), "temp 临时", False),
    (re.compile(r"^[^/]+/apm_record/"), "apm_record 监控", False),
    (re.compile(r"^all_users/"), "all_users 公共", False),
    (re.compile(r"^Backup/"), "Backup 备份", False),
    (re.compile(r"^old_backup/"), "old_backup 旧备份", False),
]


def area_of(rel):
    for rx, name, scoped in AREAS:
        if rx.match(rel):
            return name, scoped
    return "其它", False


def stem_of(name):
    return re.split(r"[_.]", name, maxsplit=1)[0]


def load_refs(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        d = json.load(f)
    ids = set(d["ids"])
    names = set(d["names"])
    lower = set(n.lower() for n in names)
    accts = set(d["meta"].get("accounts_with_refs") or [])
    return ids, names, lower, accts, d["meta"]


def match(base, h1, h2, ids, names, lower):
    """返回命中原因，或 None（= 孤立）。"""
    if base in names or base.lower() in lower:
        return "name"
    stem, ext = os.path.splitext(base)
    s2 = TRAIL_HEX.sub("", stem)
    if s2 != stem:
        for cand in (s2, s2 + ext):
            if cand in names or cand.lower() in lower:
                return "name-striphex"
    for tok in HEX_IN_NAME.findall(base.lower()):
        if tok in ids:
            return "id-in-name"
    if h1 in ids:
        return "md5-raw"
    if h2 != h1 and h2 in ids:
        return "md5-norm"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inv", required=True)
    ap.add_argument("--refs", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    t0 = time.time()
    ids, names, lower, accts_with_refs, refmeta = load_refs(a.refs)
    os.makedirs(a.out, exist_ok=True)
    print("引用集：ids=%d names=%d 可查账号=%s" % (len(ids), len(names), sorted(accts_with_refs) or ["(单账号树)"]))

    rows = []
    with open(a.inv, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 6:
                continue
            rel, size, mtime, h1, h2, kind = p[0], int(p[1]), int(p[2]), p[3], p[4], p[5]
            if size < 0:
                continue
            nlink = int(p[6]) if len(p) > 6 and p[6].isdigit() else 0
            inode = int(p[7]) if len(p) > 7 and p[7].isdigit() else 0
            rows.append((rel, size, mtime, h1, h2, kind, nlink, inode))
    print("清单：%d 个文件，%.1f GB" % (len(rows), sum(r[1] for r in rows) / 2**30))

    # inode -> 该 inode 下我们扫到的所有路径（用来识别「多条链接指向同一份数据」）
    by_inode = collections.defaultdict(list)
    inode_of = {}
    for r in rows:
        inode_of[r[0]] = r[7]
        if r[7]:
            by_inode[r[7]].append(r[0])

    # ---------------------------------------------------------------- 匹配
    stat = collections.Counter()
    by_area = collections.Counter()
    by_area_orphan = collections.Counter()
    by_kind = collections.Counter()
    orphan_kind = collections.Counter()
    orphan_acct = collections.Counter()
    orphan_acct_bytes = collections.Counter()
    reasons = collections.Counter()
    orphans = []
    indexed = set()
    for rel, size, mtime, h1, h2, kind, nlink, inode in rows:
        area, scoped = area_of(rel)
        acct = rel.split("/", 1)[0] if "/" in rel else "-"
        if accts_with_refs and acct not in accts_with_refs and "/" in rel:
            acct = "(无库可查) " + acct
        by_area[area] += 1
        by_kind[kind] += 1
        stat["bytes_%s" % area] += size
        base = rel.rsplit("/", 1)[-1]
        why = match(base, h1, h2, ids, names, lower)
        if why:
            reasons[why] += 1
            indexed.add(rel)
            continue
        if scoped:
            orphans.append((rel, size, mtime, kind, h2, area, acct))
            by_area_orphan[area] += 1
            orphan_kind[kind] += 1
            orphan_acct[acct] += 1
            orphan_acct_bytes[acct] += size
            stat["orphan_bytes_%s" % area] += size
    # 孤立的「链接」：数据本身被别的路径引用（同 inode），只是这条路径没被索引
    linked_orphans = []
    real_orphans = []
    for o in orphans:
        rel = o[0]
        inode = inode_of.get(rel, 0)
        if inode and any(p != rel and p in indexed for p in by_inode.get(inode, ())):
            linked_orphans.append(o)
        else:
            real_orphans.append(o)
    print("有索引 %d，孤立(范围内) %d = 真孤立 %d + 只是链接未被索引 %d"
          % (len(indexed), len(orphans), len(real_orphans), len(linked_orphans)))
    print("命中原因：", dict(reasons))
    print("孤立按账号：", dict(orphan_acct))

    # ---------------------------------------------------------------- 重复
    # 精确重复：落盘字节完全相同
    ex = collections.defaultdict(list)
    for r in rows:
        ex[(r[1], r[3])].append(r)
    # 规范化重复：同一内容的不同封装
    nm = collections.defaultdict(list)
    for r in rows:
        nm[r[4]].append(r)

    dup_groups_exact = {k: v for k, v in ex.items() if len(v) > 1}
    dup_groups_norm = {k: v for k, v in nm.items() if len(v) > 1}

    def pick_keep(v):
        """留谁：优先留下**有索引**的；其次路径字典序最小。"""
        return sorted(v, key=lambda x: (x[0] not in indexed, x[0]))[0]

    def reclaim(v):
        """**按 inode 算**：把这一组全部改成指向同一个 inode 之后能省下的字节数。

        实测确认：客户端自己已经硬链接了一部分重复（同组 6 个 `.pptx` 共享一个 inode、
        `st_nlink=6`）⇒ 按文件个数算会**高估**。正确口径是
        `(不同 inode 个数 - 1) × 大小`。
        """
        inodes = {r[7] for r in v if r[7]} or {id(r) for r in v}
        return (len(inodes) - 1) * v[0][1], len(inodes)

    def summarize(groups, label):
        nfile = sum(len(v) - 1 for v in groups.values())
        nbytes = sum(reclaim(v)[0] for v in groups.values())
        full = sum(1 for v in groups.values() if reclaim(v)[1] == 1)
        print("%s：%d 组，冗余文件 %d 个；**按 inode 可回收 %.2f GB**"
              "（其中 %d 组已经全部硬链接、实际可回收 0）"
              % (label, len(groups), nfile, nbytes / 2**30, full))
        return nfile, nbytes, full

    ex_f, ex_b, ex_full = summarize(dup_groups_exact, "精确重复（同 md5_raw）")
    nm_f, nm_b, nm_full = summarize(dup_groups_norm, "规范化重复（同 md5_norm）")

    # 规范化比精确多出来的，就是「同一内容、不同封装」
    extra = {k: v for k, v in dup_groups_norm.items()
             if len({r[3] for r in v}) > 1}
    cross_f = sum(len(v) - 1 for v in extra.values())
    cross_b = sum(reclaim(v)[0] for v in extra.values())
    print("其中跨封装（内容相同、封装不同）：%d 组，%d 个文件 / %.2f GB"
          % (len(extra), cross_f, cross_b / 2**30))

    # ---------------------------------------------------------------- 落盘
    def csvw(name):
        f = open(os.path.join(a.out, name), "w", encoding="utf-8", newline="")
        return f, csv.writer(f)

    # 给每个「真孤立」打 ABC 类：
    #   A = 同内容在别处**有索引**（删掉绝对安全）
    #   B = 只跟其它孤立文件重复（内容在这棵树里还有备份，但没有库线索）
    #   C = 完全独一份（库里没有任何线索）
    cls = {}
    for key, v in dup_groups_norm.items():
        paths = [r[0] for r in v]
        present = [p for p in paths if p in set(o[0] for o in real_orphans)]
        if not present:
            continue
        has_idx = any(p in indexed for p in paths)
        for p in present:
            cls[p] = "A" if has_idx else "B"
    for o in real_orphans:
        cls.setdefault(o[0], "C")
    clsc = collections.Counter(cls[o[0]] for o in real_orphans)
    clsb = collections.Counter()
    for o in real_orphans:
        clsb[cls[o[0]]] += o[1]
    print("真孤立分类：A(有索引孪生) %d 个 / %.2f GB；B(仅孤立间重复) %d 个 / %.2f GB；"
          "C(完全独一份) %d 个 / %.2f GB"
          % (clsc["A"], clsb["A"] / 2**30, clsc["B"], clsb["B"] / 2**30,
             clsc["C"], clsb["C"] / 2**30))

    # 孤立文件（区分「真孤立」与「只是这条链接没被索引、数据另有引用」）
    f, w = csvw("orphans.csv")
    w.writerow(["relpath", "size", "mtime", "kind", "md5_norm", "area", "account", "orphan_kind"])
    linked = set(o[0] for o in linked_orphans)
    for r in sorted(orphans, key=lambda x: -x[1]):
        w.writerow(list(r) + ["link-only" if r[0] in linked else "true"])
    f.close()

    # 真孤立单独一份，按大小倒序（带 ABC 类）
    f, w = csvw("orphans-true.csv")
    w.writerow(["relpath", "size", "mtime", "kind", "md5_norm", "area", "account", "cls"])
    for r in sorted(real_orphans, key=lambda x: -x[1]):
        w.writerow(list(r) + [cls[r[0]]])
    f.close()

    ordered = sorted(dup_groups_norm.items(), key=lambda kv: -reclaim(kv[1])[0])

    # 重复组
    f, w = csvw("dup-groups.csv")
    w.writerow(["group", "n_files", "n_inodes", "keeper_size", "reclaim_bytes",
                "already_hardlinked", "cross_wrap", "area", "all_indexed", "paths"])
    for gid, (key, v) in enumerate(ordered, 1):
        keeper = pick_keep(v)
        wasted, nino = reclaim(v)
        cross = "Y" if len({r[3] for r in v}) > 1 else ""
        allidx = "Y" if all(r[0] in indexed for r in v) else ""
        w.writerow([gid, len(v), nino, keeper[1], wasted,
                    "Y" if nino == 1 else "", cross, area_of(keeper[0])[0],
                    allidx, " | ".join(r[0] for r in v)])
    f.close()

    # 重复文件明细
    f, w = csvw("dup-files.csv")
    w.writerow(["relpath", "size", "kind", "md5_raw", "inode", "nlink", "group",
                "role", "indexed", "area"])
    for gid, (key, v) in enumerate(ordered, 1):
        keeper = pick_keep(v)
        for r in v:
            w.writerow([r[0], r[1], r[5], r[3], r[7], r[6], gid,
                        "keep" if r is keeper else "dup",
                        "Y" if r[0] in indexed else "", area_of(r[0])[0]])
    f.close()

    # 「既重复又孤立」—— 最安全的清理候选
    f, w = csvw("dup-and-orphan.csv")
    w.writerow(["relpath", "size", "kind", "md5_raw", "group", "area"])
    n_dao = 0
    for gid, (key, v) in enumerate(ordered, 1):
        keeper = pick_keep(v)
        for r in v:
            if r is keeper or r[0] in indexed:
                continue
            w.writerow([r[0], r[1], r[5], r[3], gid, area_of(r[0])[0]])
            n_dao += 1
    f.close()

    stats = {
        "files": len(rows),
        "bytes": sum(r[1] for r in rows),
        "indexed": len(indexed),
        "orphans": len(orphans),
        "orphans_true": len(real_orphans),
        "orphans_link_only": len(linked_orphans),
        "orphan_bytes": sum(r[1] for r in orphans),
        "orphan_bytes_true": sum(r[1] for r in real_orphans),
        "orphan_class_files": dict(clsc),
        "orphan_class_bytes": dict(clsb),
        "bytes_on_distinct_inodes": sum(r[1] for r in rows if r[7] and by_inode[r[7]][0] == r[0]),
        "by_area": dict(by_area),
        "by_area_orphan": dict(by_area_orphan),
        "orphan_by_account": dict(orphan_acct),
        "orphan_bytes_by_account": dict(orphan_acct_bytes),
        "accounts_with_refs": sorted(accts_with_refs),
        "by_kind": dict(by_kind),
        "orphan_kind": dict(orphan_kind),
        "reasons": dict(reasons),
        "dup_exact_groups": len(dup_groups_exact),
        "dup_exact_redundant_files": ex_f,
        "dup_exact_reclaim_bytes": ex_b,
        "dup_exact_fully_hardlinked_groups": ex_full,
        "dup_norm_groups": len(dup_groups_norm),
        "dup_norm_redundant_files": nm_f,
        "dup_norm_reclaim_bytes": nm_b,
        "dup_norm_fully_hardlinked_groups": nm_full,
        "cross_wrap_groups": len(extra),
        "cross_wrap_files": cross_f,
        "cross_wrap_bytes": cross_b,
        "dup_and_orphan_files": n_dao,
        "refs_meta": refmeta,
        "seconds": round(time.time() - t0, 1),
    }
    with open(os.path.join(a.out, "stats.json"), "w", encoding="utf-8") as fo:
        json.dump(stats, fo, ensure_ascii=False, indent=2)
    print("stats -> %s/stats.json（%.1fs）" % (a.out, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
