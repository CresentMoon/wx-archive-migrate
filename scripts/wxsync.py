#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxsync.py — 给账号下的所有库拍状态快照，并做前后差分。

这是整套方法里最有用的一件工具
------------------------------
不要靠猜列语义、也不要靠反编译去猜 SQL。做法是：**让客户端自己做一次我们想学的操作，
然后看它到底写了什么。**

    1) 客户端先跑起来（先别动手）
    2) python3 wxsync.py snap before --data-root <DATA_ROOT> --key-env <MSG_DB>=<VAR>
    3) 在客户端里做那件事（例如：从手机发一条语音给「文件传输助手」）
    4) 让客户端**干净退出**（这样 -wal 会被检查点进主库，diff 才看得到）
    5) python3 wxsync.py snap after  --data-root <DATA_ROOT> --key-env <MSG_DB>=<VAR>
    6) python3 wxsync.py diff before.json after.json

第 6 步会告诉你：客户端把新数据写进了哪个库、哪张表、哪些列。
本仓库里**几乎全部列语义**（`Sequence = CreateTime*1000`、语音的 `Reserved1` 是 2、
`BytesExtra` 子类型含义、语音落在 `MediaMSG0.Media` 而不是 `MediaInfo`……）
都是这么测出来的，比从二进制里猜可靠得多。

记什么
------
* **文件层**：账号目录下所有 `.db` / `.db-wal` / `.db-shm` 的大小与 mtime。
  这一层**不需要密钥**，所以哪怕你一个密钥都没有，也能看出客户端刚碰了哪个库。
* **表层**（只对给了 `--key` 的库）：每张表的行数；`MSG` / `Media` / `MediaInfo` /
  `ChatCRVoice` 另外记**内容指纹** —— 用来发现「行数没变但内容变了」这种情况
  （客户端经常只改几个列，行数一动不动）。

关于内容指纹的一个真实的坑
--------------------------
指纹必须用 **`hashlib.md5`**，**不能用 Python 内置的 `hash()`**：内置 `hash()` 对
str/bytes 是**按进程随机加盐**的（`PYTHONHASHSEED`），同一个库在两次运行里会得到不同的值，
于是每次 diff 都报「全变了」，把真正的差异淹掉。这个坑我们踩过，所以共享库
`wxcom.snapshot()` 用的就是 md5（`repr(rows)` 的前十位）。

CLI 契约
--------
    wxsync.py snap <标签> [选项]        # 拍快照；默认输出 ./wxsync_<标签>.json
    wxsync.py diff <前.json> <后.json>  # 打印文件差异与表行数/内容差异

    --data-root <DIR>            账号目录（含 Msg/…），必需
    --key <路径>=<AES_KEY>       给某个库配密钥，可重复（路径相对 --data-root）
    --key-env <路径>=<VAR>       同上，但密钥从环境变量读（避免进 shell 历史）
    --out <FILE>                 snap 的输出文件

真正的实现在共享库 `wxcom/`（`snapshot()` / `diff()`）—— 本文件只负责命令行。
"""
import argparse
import os
import sys

try:
    import wxcom as _wxcom
except ImportError:                                    # pragma: no cover
    _wxcom = None

USAGE = """用法：
    wxsync.py snap <标签> [选项]
    wxsync.py diff <前.json> <后.json>

snap 选项：
    --data-root <DIR>          账号目录（含 Msg/…），必需
    --key <路径>=<AES_KEY>     给某个库配 AES 密钥（可重复；路径相对 --data-root）
    --key-env <路径>=<VAR>     同上，密钥从环境变量读
    --out <FILE>               输出文件（默认 ./wxsync_<标签>.json）

典型用法（学"客户端到底写了什么"）：
    python3 wxsync.py snap before --data-root <DATA_ROOT> --key-env <MSG_DB>=WX_MSG_KEY
    #  …在客户端里做一次操作，然后干净退出…
    python3 wxsync.py snap after  --data-root <DATA_ROOT> --key-env <MSG_DB>=WX_MSG_KEY
    python3 wxsync.py diff before.json after.json"""


def _need(*names):
    if _wxcom is None:
        raise SystemExit('缺少共享库：与本脚本同目录的 wxcom/（见 scripts/wxcom/__init__.py）')
    missing = [n for n in names if getattr(_wxcom, n, None) is None]
    if missing:
        raise SystemExit('wxcom 缺少这些接口：%s' % ', '.join(missing))
    return _wxcom


def parse_keys(pairs, env_pairs, root):
    """`--key` / `--key-env` -> `{相对 --data-root 的路径: AES 密钥 bytes}`。

    共享库的 `snapshot(root, out, keyed=…)` 会自己把 keyed 的键和 root 拼起来，
    所以这里**必须交相对路径**（绝对路径会被拼成 nonsense，静默拿不到表信息）。
    """
    root = os.path.abspath(os.path.expanduser(root))
    keys = {}
    for raw, is_env in ([(p, False) for p in (pairs or [])]
                        + [(p, True) for p in (env_pairs or [])]):
        if '=' not in raw:
            raise SystemExit('--key/--key-env 要写成 <路径>=<密钥或变量名>，收到：%r' % raw)
        rel, val = raw.split('=', 1)
        if is_env:
            v = os.environ.get(val)
            if not v:
                raise SystemExit('环境变量 %s 是空的' % val)
            val = v
        key = _need('aes_from_hex').aes_from_hex(val, '密钥 %s' % rel)
        if os.path.isabs(rel):
            # 允许写绝对路径，但必须落在 --data-root 里面，否则退回相对路径
            if not os.path.abspath(rel).startswith(root + os.sep):
                raise SystemExit('%s 不在 --data-root 里面，共享库会拼错路径' % rel)
            rel = os.path.relpath(os.path.abspath(rel), root)
        rel = rel.replace(os.sep, '/')
        if not os.path.exists(os.path.join(root, rel)):
            raise SystemExit('找不到库：%s（相对 --data-root；路径写对了吗）' % rel)
        keys[rel] = key
    return keys


def cmd_snap(args):
    wx = _need('snapshot')
    keys = parse_keys(args.key, args.key_env, args.data_root)
    out = args.out or ('wxsync_%s.json' % args.label)
    if keys:
        print('带密钥的库（会额外记表行数与内容指纹）：')
        for rel in sorted(keys):
            print('   %s' % rel)
    else:
        print('没有给 --key/--key-env —— 只做文件层快照（大小/mtime 仍能指出"客户端动了哪个库"）')
    wx.snapshot(args.data_root, out, keyed=keys)
    print('快照 -> %s' % out)
    print('下一步：在客户端里做那一次操作 → 干净退出 → snap 另一份 → diff。')
    return 0


def cmd_diff(args):
    wx = _need('diff')
    n = wx.diff(args.before, args.after)
    print('\n差异条目数：%d %s' % (n, '（完全一致）' if n == 0 else ''))
    if n:
        print('提示：库文件的 size/mtime 变了但表行数没变，通常是 --wal 还没被检查点 ——')
        print('      先让客户端「托盘右键 → 退出微信」，再重拍 after 那份。')
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    sub = argv[0]
    if sub == 'snap':
        p = argparse.ArgumentParser(prog='wxsync.py snap',
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    description=USAGE)
        p.add_argument('label', help='标签（用于默认输出文件名）')
        p.add_argument('--data-root', required=True, help='账号目录（含 Msg/…）')
        p.add_argument('--key', action='append', default=[],
                       help='<相对路径>=<AES_KEY>，可重复')
        p.add_argument('--key-env', action='append', default=[],
                       help='<相对路径>=<环境变量名>，可重复')
        p.add_argument('--out', default='', help='输出文件')
        return cmd_snap(p.parse_args(argv[1:]))
    if sub == 'diff':
        p = argparse.ArgumentParser(prog='wxsync.py diff',
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    description=USAGE)
        p.add_argument('before', help='前一份快照 json')
        p.add_argument('after', help='后一份快照 json')
        return cmd_diff(p.parse_args(argv[1:]))
    print('未知子命令 %r' % sub)
    print(USAGE)
    return 2


if __name__ == '__main__':
    sys.exit(main())
