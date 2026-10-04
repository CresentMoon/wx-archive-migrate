#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「内容重复的附件」折叠成硬链接 —— **不改路径、不改文件名、不碰任何 `.db`**。

依据（见 `docs/detail/attachments.md`）
--------------------------------------------------------------------------
* 客户端定位文件用的是 **目录 + 文件名**，而目录由**消息时间**（`msg/file/<年-月>/`）
  或**收件会话**（`msg/attach/<会话md5>/…`）决定 —— **两个都改不了**。
  所以「一个内容只留一个文件名」在 74% 的重复上做不到；
  **能做且零风险的只有一件事：让 N 个名字指向同一份物理数据。**
* 客户端**自己就是这么干的**（官方迁移时对大二进制建 NTFS 硬链接；
  `hardlink.db` 就是它自己的去重账本）。本工具与官方行为**同构**。
* 硬链接对客户端**完全透明** —— 它看到的还是普通文件。
* **可逆**：`rollback` 能从 keeper 重建每一个名字。

命令
----
    plan     --dup-files <dup-files.csv> --root <数据根> --out plan.json
             [--areas msg] [--limit-groups N] [--inv <inventory.tsv[.gz]>]
    apply    --plan plan.json --manifest manifest.json [--yes] [--limit-groups N]
             [--no-content-check]
    verify   --manifest manifest.json
    rollback --manifest manifest.json [--yes]

**两条实测确认的坑**
-----------------------
1. **`st_ino` 不能跨平台比**：同一个文件，Linux 的 drvfs 与 Windows 原生 st_ino
   **恒定差 2**（例：`281474979371970` vs `281474979371968`）。
   `nlink` 两边**完全一致**。所以本工具**只信自己现场 stat 的结果**，
   `dup-files.csv` 里的 inode 列一概不用。
   **因此 plan 与 apply 必须在同一个平台、同一台机器上跑。**
2. **`st_ino` 会被复用**：仅凭 inode 相等不能证明内容是同一份。所以 apply 时额外做
   **头尾采样指纹**（size + 前 64 KB + 后 64 KB 的 md5）复核 dup 与 keeper。

纪律（与项目根 `AGENTS.md` 一致）
---------------------------------
* **默认 dry-run**：`apply` / `rollback` 不加 `--yes` 只打印将要做什么。
* **先建链接、再原子替换**（`os.link` → `os.replace`）：全程**不存在"文件不见了"的窗口**。
  绝不"先删后建"。
* 任何一个文件失败**只跳过它并记 log**，不中断整批。
* 不写、不改、不删任何 `.db`；不移动任何路径。
* 临时名只在**同目录**里出现（`<名>.hl.tmp` / `<名>.rb.tmp`），结束后不留残留。

运行位置
--------
推荐 **Windows 侧 `python.exe`**（NTFS 语义最稳、快）。也可在 Linux 侧对 `/mnt/c` 跑，
但**同一轮 plan/apply 不能换平台**。

注意：从 WSL 调用 Windows 侧 python 时**必须加 `-X utf8`**：WSL interop **不转发**
`PYTHONIOENCODING` 这类环境变量前缀，Windows Python 的 stdout 会退化成 **GBK（代码页 936）**，
中文输出全是乱码（而乱码会把 `不同inode` 显示成看着像 `同inode`，据此判断就是错的）。

    /mnt/c/.../python.exe -X utf8 wx4_attach_dedup.py apply ...
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import stat
import sys
import time

TMP_SUFFIX = ".hl.tmp"
RB_SUFFIX = ".rb.tmp"
CHUNK = 1 << 16          # 头尾各 64 KB 采样


def _local(p):
    """Windows 上 `/mnt/c/…` → `C:\\…`；Linux 上原样返回（AGENTS.md §3 的坑）。"""
    if sys.platform == "win32":
        m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", p or "")
        if m:
            return "%s:\\%s" % (m.group(1).upper(), m.group(2).replace("/", "\\"))
    return p


def to_wsl_path(p):
    """`C:\\a\\b` → `/mnt/c/a/b`。"""
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return "/mnt/%s/%s" % (p[0].lower(), p[2:].replace("\\", "/").lstrip("/"))
    return p


def save(p, o):
    d = os.path.dirname(os.path.abspath(p))
    if d:
        os.makedirs(d, exist_ok=True)
    with io.open(p, "w", encoding="utf-8") as fh:
        json.dump(o, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def load(p):
    with io.open(p, encoding="utf-8") as fh:
        return json.load(fh)


def open_maybe_gz(p):
    if p.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(p, "rb"), encoding="utf-8")
    return io.open(p, encoding="utf-8")


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.2f %s" % (n, u)
        n /= 1024.0


def fingerprint(path, size=None):
    """size + 头 64 KB + 尾 64 KB 的 md5 —— 便宜的「这两个文件现在是同一份吗」判据。

    不做全文件哈希：24 GB 全量要几分钟，而头尾采样对"被改写过"这件事足够敏感，
    真正的全量等价性已经由 `wx4_attach_audit.py` 的 `md5_norm` 保证过。
    """
    if size is None:
        size = os.path.getsize(path)
    h = hashlib.md5()
    h.update(b"len=%d;" % size)
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
        if size > 2 * CHUNK:
            f.seek(-CHUNK, os.SEEK_END)
            h.update(f.read(CHUNK))
    return h.hexdigest()


def snapshot(path):
    """现场 stat —— 只信本平台的结果。"""
    st = os.lstat(path)
    return {"exists": True, "size": st.st_size, "inode": st.st_ino,
            "nlink": st.st_nlink, "mtime": int(st.st_mtime),
            "is_file": os.path.isfile(path) and not os.path.islink(path)}


# --------------------------------------------------------------------------- plan
def read_inventory_sizes(path):
    """relpath -> size（仅用于交叉核对，可选）"""
    out = {}
    if not path:
        return out
    with open_maybe_gz(path) as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2:
                out[p[0]] = int(p[1])
    return out


def area_ok(rel, areas, accounts):
    """`rel` 形如 `<账号>/msg/file/<年-月>/<名>`。

    * `accounts` 非空时，**第一段必须是其中之一** —— 否则会把 `wxid_*` 另两个账号也卷进来
      （它们的库我们解不开，虽然硬链接本身与库无关，但第一轮先把爆炸半径压到最小）。
    * `areas` 匹配路径里的目录名（`msg` / `attach` / `file` / …）。
    """
    q = rel.split("/")
    if accounts and (not q or q[0] not in accounts):
        return False
    if not areas:
        return True
    return any(a in set(q[1:4]) for a in areas)


def cmd_plan(args):
    root = _local(args.root)
    sizes = read_inventory_sizes(args.inv)
    groups, order = {}, []
    with io.open(args.dup_files, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            g = int(r["group"])
            if g not in groups:
                groups[g] = {"gid": g, "keeper": None, "keeper_md5": None, "dups": []}
                order.append(g)
            if r.get("role") == "keep" and groups[g]["keeper"] is None:
                groups[g]["keeper"] = r["relpath"]
                groups[g]["keeper_md5"] = (r.get("md5_raw") or "").lower()
            else:
                groups[g]["dups"].append((r["relpath"], (r.get("md5_raw") or "").lower()))

    areas = [a for a in (args.areas or "").split(",") if a]
    accounts = [a for a in (args.accounts or "").split(",") if a]
    out = []
    skip_area = skip_gone = skip_noop = skip_bad = skip_cross = 0
    saved = 0
    problems = []
    for g in order:
        e = groups[g]
        if not e["keeper"] or not e["dups"]:
            skip_bad += 1
            continue
        if not area_ok(e["keeper"], areas, accounts) or \
           not all(area_ok(d[0], areas, accounts) for d in e["dups"]):
            skip_area += 1
            continue
        kp = os.path.join(root, e["keeper"].replace("/", os.sep))
        try:
            ks = snapshot(kp)
        except OSError as ex:
            skip_gone += 1
            problems.append({"path": e["keeper"], "why": "keeper stat: %s" % ex})
            continue
        if not ks["is_file"] or ks["size"] == 0:
            skip_bad += 1
            continue
        if args.inv and e["keeper"] in sizes and sizes[e["keeper"]] != ks["size"]:
            problems.append({"path": e["keeper"], "why": "size 与清单不符"})
            skip_bad += 1
            continue
        dups = []
        for dpath, dmd5 in e["dups"]:
            # ★★ 只折叠**逐字节相同**的文件（md5_raw 一致）。
            #    audit 的分组口径是 `md5_norm`（解开容器后相同），它会把
            #    「同一张图：明文一份 + XOR 加密的 `_W.dat` 一份」归进同一组 ——
            #    那种情况**绝对不能**硬链接：一旦共享 inode，加密容器的字节就被
            #    明文替换，客户端会解不出来。这类"跨封装"重复本轮**一律跳过**。
            if not e["keeper_md5"] or dmd5 != e["keeper_md5"]:
                skip_cross += 1
                continue
            dp = os.path.join(root, dpath.replace("/", os.sep))
            try:
                ds = snapshot(dp)
            except OSError:
                continue
            if not ds["is_file"]:
                continue
            if ds["inode"] == ks["inode"]:          # 已经同 inode，无需处理
                continue
            if ds["size"] != ks["size"]:
                problems.append({"path": dpath, "why": "size %d != keeper %d" % (ds["size"], ks["size"])})
                continue
            if args.inv and dpath in sizes and sizes[dpath] != ds["size"]:
                problems.append({"path": dpath, "why": "size 与清单不符"})
                continue
            dups.append({"path": dpath, "size": ds["size"], "inode": ds["inode"],
                         "nlink": ds["nlink"], "mtime": ds["mtime"], "md5_raw": dmd5})
        if not dups:
            skip_noop += 1
            continue
        # ★ 记账口径：**按 inode 算**，不是按文件个数。
        #   同一 inode 上的第 2、3 个名字折叠过来**不省任何空间**（它们本来就共享数据）；
        #   只有「这个 inode 上的最后一个名字搬走」才真正释放它那份 size。
        #   所以：每个非 keeper inode 只计一次 size。这与 audit 的 reclaim 口径一致。
        ino_size = {}
        for d in dups:
            ino_size.setdefault(d["inode"], d["size"])
        saved += sum(v for k, v in ino_size.items() if k != ks["inode"])
        out.append({"gid": g, "size": ks["size"],
                    "keeper": {"path": e["keeper"], "size": ks["size"], "md5_raw": e["keeper_md5"],
                               "inode": ks["inode"], "nlink": ks["nlink"], "mtime": ks["mtime"]},
                    "dups": dups})
        if args.limit_groups and len(out) >= args.limit_groups:
            break

    plan = {"meta": {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "root": args.root, "platform": sys.platform,
                     "dup_files": os.path.abspath(args.dup_files),
                     "inventory": os.path.abspath(args.inv) if args.inv else None,
                     "areas": areas, "accounts": accounts,
                     "groups": len(out),
                     "dup_files_count": sum(len(g["dups"]) for g in out),
                     "expected_saved_bytes": saved,
                     "skipped_by_area": skip_area,
                     "skipped_already_hardlinked": skip_noop,
                     "skipped_missing": skip_gone,
                     "skipped_bad": skip_bad,
                     "skipped_cross_wrapper": skip_cross,
                     "problems": problems[:200],
                     "problems_total": len(problems)},
            "groups": out}
    save(args.out, plan)
    m = plan["meta"]
    print("组 %d / 待折叠 %d 个 / 预期释放 %s" % (m["groups"], m["dup_files_count"], human(saved)))
    print("跳过：区域外 %d，已同 inode %d，跨封装(字节不同) %d，文件不存在 %d，不合格 %d；问题 %d"
          % (skip_area, skip_noop, skip_cross, skip_gone, skip_bad, len(problems)))
    print("plan: %s" % args.out)
    return 0


# -------------------------------------------------------------------------- apply
def cmd_apply(args):
    plan = load(args.plan)
    root = _local(plan["meta"]["root"])
    if plan["meta"].get("platform") != sys.platform:
        print("!! plan 是在 %s 上做的，现在在 %s 上跑 —— inode 不可跨平台比，已中止"
              % (plan["meta"].get("platform"), sys.platform))
        return 2
    do = bool(args.yes)
    man = {"meta": {"applied": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "plan": os.path.abspath(args.plan), "root": plan["meta"]["root"],
                    "platform": sys.platform, "dry_run": not do,
                    "content_check": not args.no_content_check},
           "folded": [], "skipped": [], "errors": [],
           "bytes_saved": 0, "groups_done": 0, "groups_total": len(plan["groups"])}
    gl = args.limit_groups or 0
    for gi, g in enumerate(plan["groups"]):
        if gl and gi >= gl:
            break
        kp = os.path.join(root, g["keeper"]["path"].replace("/", os.sep))
        try:
            ks = snapshot(kp)
        except OSError as e:
            man["errors"].append({"keep": g["keeper"]["path"], "why": "keeper stat: %s" % e})
            continue
        if ks["inode"] != g["keeper"]["inode"]:
            man["errors"].append({"keep": g["keeper"]["path"],
                                  "why": "keeper inode 变了（%s -> %s）"
                                         % (g["keeper"]["inode"], ks["inode"])})
            continue
        if ks["size"] != g["keeper"]["size"]:
            man["errors"].append({"keep": g["keeper"]["path"],
                                  "why": "keeper size 变了（%s -> %s）"
                                         % (g["keeper"]["size"], ks["size"])})
            continue
        kfp = None
        done = 0
        # 按 inode 记账：每个非 keeper inode 只有在其**全部名字**都成功搬走后，才计一次 size。
        by_ino = {}
        for d in g["dups"]:
            by_ino.setdefault(d["inode"], []).append(d["path"])
        ok_paths = set()
        for d in g["dups"]:
            dp = os.path.join(root, d["path"].replace("/", os.sep))
            tmp = dp + TMP_SUFFIX
            try:
                if not os.path.isfile(dp):
                    man["skipped"].append({"dup": d["path"], "why": "不存在"})
                    continue
                ds = snapshot(dp)
                if ds["inode"] == ks["inode"]:
                    man["skipped"].append({"dup": d["path"], "why": "已是同 inode"})
                    continue
                if ds["size"] != ks["size"]:
                    man["errors"].append({"dup": d["path"], "why": "size 不符"})
                    continue
                if not args.no_content_check:
                    if kfp is None:
                        kfp = fingerprint(kp, ks["size"])
                    if fingerprint(dp, ds["size"]) != kfp:
                        man["errors"].append({"dup": d["path"], "why": "头尾指纹与 keeper 不符"})
                        continue
                if not do:
                    man["folded"].append({"gid": g["gid"], "keep": g["keeper"]["path"],
                                          "dup": d["path"], "dry_run": True})
                    ok_paths.add(d["path"])
                    done += 1
                    continue
                if os.path.exists(tmp):
                    # 上一轮失败留下的残骸。它**必然**是指向 keeper 的硬链接（nlink>=2），
                    # 且原文件仍在 —— 清掉继续。⚠️ 若 keeper 本身是只读，残骸也继承只读，
                    # 直接 os.remove 会报 WinError 5，所以**必须先清只读位**。
                    try:
                        os.chmod(tmp, os.lstat(tmp).st_mode | stat.S_IWRITE)
                        os.remove(tmp)
                        man["tmp_cleaned"] = man.get("tmp_cleaned", 0) + 1
                    except OSError as e2:
                        man["errors"].append({"dup": d["path"], "why": "清理残骸失败：%s" % e2})
                        continue
                os.link(kp, tmp)                      # ① 先建链接（失败时原文件还在）
                if os.lstat(tmp).st_ino != ks["inode"]:
                    os.remove(tmp)
                    man["errors"].append({"dup": d["path"], "why": "建链后 inode 不一致"})
                    continue
                # ② Windows 的 READONLY 属性会让 os.replace 报 WinError 5。
                #    只清掉**即将被淘汰的那个 inode** 上的只读位；替换之后这条路径
                #    指向 keeper 的 inode，属性随 keeper（不会影响 keeper 自己）。
                was_ro = not (os.lstat(dp).st_mode & stat.S_IWRITE)
                if was_ro:
                    os.chmod(dp, os.lstat(dp).st_mode | stat.S_IWRITE)
                os.replace(tmp, dp)                   # ③ 原子替换
                if os.lstat(dp).st_ino != ks["inode"]:
                    man["errors"].append({"dup": d["path"], "why": "替换后 inode 不一致"})
                    continue
                man["folded"].append({"gid": g["gid"], "keep": g["keeper"]["path"],
                                      "dup": d["path"], "old_inode": ds["inode"],
                                      "old_size": ds["size"], "old_mtime": ds["mtime"],
                                      "new_inode": ks["inode"], "target_was_readonly": was_ro})
                ok_paths.add(d["path"])
                done += 1
            except OSError as e:
                man["errors"].append({"dup": d["path"], "why": "%s: %s" % (type(e).__name__, e)})
                if os.path.exists(tmp):
                    try:
                        os.chmod(tmp, os.lstat(tmp).st_mode | stat.S_IWRITE)
                        os.remove(tmp)
                    except OSError:
                        pass
        for ino, paths in by_ino.items():
            if ino == ks["inode"] or not all(p in ok_paths for p in paths):
                continue
            man["bytes_saved"] += next((x["size"] for x in g["dups"] if x["inode"] == ino), 0)
        if done:
            man["groups_done"] += 1
        if (gi + 1) % 500 == 0:
            print("  … %d/%d 组" % (gi + 1, len(plan["groups"])), flush=True)
    save(args.manifest, man)
    print("%s：处理 %d/%d 组，折叠 %d 个，%s；跳过 %d，异常 %d"
          % ("DRY-RUN" if not do else "完成", man["groups_done"], man["groups_total"],
             len(man["folded"]), human(man["bytes_saved"]),
             len(man["skipped"]), len(man["errors"])))
    for e in man["errors"][:5]:
        print("   异常：%s" % e)
    print("manifest: %s" % args.manifest)
    return 0


# ------------------------------------------------------------------------- verify
def cmd_verify(args):
    import collections
    man = load(args.manifest)
    root = _local(man["meta"]["root"])
    bad = []
    per_group = collections.defaultdict(set)
    for e in man["folded"]:
        kp = os.path.join(root, e["keep"].replace("/", os.sep))
        dp = os.path.join(root, e["dup"].replace("/", os.sep))
        try:
            ks, ds = os.lstat(kp), os.lstat(dp)
        except OSError as ex:
            bad.append({"dup": e["dup"], "why": "stat: %s" % ex})
            continue
        if ks.st_ino != ds.st_ino:
            bad.append({"dup": e["dup"], "why": "不同 inode %s vs %s" % (ks.st_ino, ds.st_ino)})
        elif ks.st_size != ds.st_size:
            bad.append({"dup": e["dup"], "why": "size 不一致"})
        else:
            per_group[e["gid"]].add(ks.st_ino)
    multi = {g: sorted(s) for g, s in per_group.items() if len(s) > 1}
    print("校验 %d 条：通过 %d，异常 %d；覆盖 %d 个组"
          % (len(man["folded"]), len(man["folded"]) - len(bad), len(bad), len(per_group)))
    if multi:
        print("!! 有 %d 个组仍存在多个 inode：" % len(multi))
        for g, s in list(multi.items())[:5]:
            print("   gid=%s inodes=%s" % (g, s))
    for b in bad[:10]:
        print("   %s  %s" % (b["dup"], b["why"]))
    return 1 if (bad or multi) else 0


# ----------------------------------------------------------------------- rollback
def cmd_rollback(args):
    man = load(args.manifest)
    root = _local(man["meta"]["root"])
    if man["meta"].get("platform") != sys.platform:
        print("!! manifest 是 %s 上做的，现在在 %s 上跑，已中止"
              % (man["meta"].get("platform"), sys.platform))
        return 2
    do = bool(args.yes)
    ok = skip = err = 0
    for e in man["folded"]:
        kp = os.path.join(root, e["keep"].replace("/", os.sep))
        dp = os.path.join(root, e["dup"].replace("/", os.sep))
        tmp = dp + RB_SUFFIX
        try:
            ks, ds = os.lstat(kp), os.lstat(dp)
            if ks.st_ino != ds.st_ino:
                skip += 1
                continue
            if not do:
                ok += 1
                continue
            if os.path.exists(tmp):
                os.remove(tmp)
            with open(kp, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    b = fi.read(1 << 20)
                    if not b:
                        break
                    fo.write(b)
            os.replace(tmp, dp)
            if e.get("old_mtime"):
                os.utime(dp, (e["old_mtime"], e["old_mtime"]))
            ok += 1
        except OSError as ex:
            err += 1
            if err <= 5:
                print("   失败 %s：%s" % (e["dup"], ex))
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    print("%s：还原 %d，跳过（已非硬链接）%d，失败 %d"
          % ("DRY-RUN" if not do else "完成", ok, skip, err))
    return 0


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--dup-files", required=True, help="audit 产出的 dup-files.csv")
    p.add_argument("--root", required=True, help="xwechat_files 的绝对路径")
    p.add_argument("--out", required=True)
    p.add_argument("--inv", help="attach-inventory.tsv[.gz]，可选，用于交叉核对 size")
    p.add_argument("--areas", default="msg", help="只处理路径里含这些目录名的组；空=全部")
    p.add_argument("--accounts", default="",
                   help="只处理这些账号目录（逗号分隔，防误伤同根下的其它账号）；空=全部")
    p.add_argument("--limit-groups", type=int, default=0)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply")
    p.add_argument("--plan", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--yes", action="store_true", help="不加就是 dry-run")
    p.add_argument("--limit-groups", type=int, default=0)
    p.add_argument("--no-content-check", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("verify")
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("rollback")
    p.add_argument("--manifest", required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_rollback)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
