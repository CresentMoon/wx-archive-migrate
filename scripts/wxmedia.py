#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxmedia.py — 按消息类型把 2.x 归档里的媒体放进 3.x 布局，并改写 BytesExtra 里的路径。

为什么需要这一步
----------------
2.x 的 `MSG.BytesExtra` 里记的是**当年那台机器的绝对路径**，所以换到新机器后，
客户端找不到文件，图片/文件/视频都显示「已被清理」／打不开。

关键实测结论：**客户端按「记录下来的路径」找媒体，不按内容哈希。**
所以路径由我们写、文件由我们放，两边一致就行。
（旁证：四个内容完全相同的附件，带着四个不同的 BytesExtra 子类型2 值 —— 那个值
不是内容 md5，没法用来匹配文件。）

各类型的布局（除注明外均为实测确认）
------------------------------------
| Type    | 位置                                                                 | 编码         |
|---------|----------------------------------------------------------------------|--------------|
| 3       | `<账号>\\FileStorage\\MsgAttach\\<md5(StrTalker)>\\Image\\<YYYY-MM>\\` 与 `.../Thumb/<YYYY-MM>/` | **XOR 0xA0** |
| 43 / 62 | `<账号>\\FileStorage\\Video\\<文件名>`（**扁平，没有月份子目录**）          | 明文         |
| 47      | `<账号>\\FileStorage\\CustomEmotion\\<NAME 前两位>\\<NAME>`，NAME = 内容 md5 大写 hex | 明文 GIF |
| 49(6)   | `<账号>\\FileStorage\\File\\<YYYY-MM>\\<文件名>`                          | 明文         |

* 视频那一层原来是**推断**，后来 91/91 全部实测可播 ⇒ 现在是实测确认。
* 表情在 3.9 自己存的时候是 `V1MMWX` 开头的加密封装，**格式未破解**；但客户端会
  **嗅探格式**，所以放明文 GIF 进去能显示。这一步**不改数据库**，纯文件放置，随时可退。
* `BytesExtra` 结构：**外层字段1 = varint 对** `{1:子类型, 2:varint}`，
  **外层字段3 = bytes 对** `{1:子类型, 2:bytes}`。本工具只改外层字段3 的子类型 3/4（路径）。
* 只处理内层 appmsg 含 `<type>6</type>` 的 Type=49；`<type>5</type>` 是链接卡片，
  它的子类型4 是封面图 md5，**绝不能动**。

CLI 契约
--------
    wxmedia.py <类型> <动作> [选项]
    类型 = images | video | stickers | files
    动作 = check（默认，只读）| build | verify | install

    check    只读：库里引用数 / 归档里找到几个 / 缺哪些（列前若干条）
    build    复制文件到目标布局，并生成改好 BytesExtra 的库（不碰客户端）
    verify   全页 HMAC + 可用区往返 + integrity + 逐行核对路径所指文件是否存在
    install  **先备份再替换**，打印备份路径与还原命令

示例
----
    python3 wxmedia.py images check --wechat-dir "<DATA_ROOT>" --account <ACCOUNT> \\
            --archive <ARCHIVE_2X> --key-env WX_MSG_KEY
    python3 wxmedia.py files build ... && python3 wxmedia.py files verify ... \\
            && python3 wxmedia.py files install ...

安装前请让客户端**干净退出**（托盘右键 → 退出微信），或用 `install_guard.sh` 包住本命令。

依赖同目录下的共享库 `wxcom/`（SQLCipher 往返、BytesExtra 编解码、DBInfo 等）。
"""
import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time

# ------------------------------------------------------------------ wxcom
# 延迟导入：wxcom 尚未就位时也要能正常打印 --help（退出码 0）。
try:
    import wxcom as _wxcom
except ImportError:                                    # pragma: no cover
    _wxcom = None

USAGE = """用法：
    wxmedia.py <类型> <动作> [选项]
    类型 = images | video | stickers | files
    动作 = check（默认，只读）| build | verify | install

必需参数：
    --wechat-dir <DATA_ROOT>   微信数据根（其下有 <账号> 目录）
    --account    <ACCOUNT>     账号目录名
    --archive    <ARCHIVE_2X>  2.x 归档的账号目录（媒体来源）
    --key-hex <AES_KEY> 或 --key-env <VAR>   MSG*.db 的 AES 密钥

常见可选参数：
    --msg-db <MSG_DB>  显式指定 MSG*.db（默认 <账号>/Msg/Multi/MSG0.db）
    --work <DIR>       工作目录（默认 ./_wxwork）
    --backup-dir <DIR> install 的备份目录
    --extra-root <DIR> 额外的文件搜索根（可重复；仅 files 用）"""


def _need(*names):
    """确认共享库在、且提供我们要用的接口；缺了就给出可操作的中文提示。"""
    if _wxcom is None:
        raise SystemExit('缺少共享库：与本脚本同目录的 wxcom/（见 scripts/wxcom/__init__.py）')
    missing = [n for n in names if getattr(_wxcom, n, None) is None]
    if missing:
        raise SystemExit('wxcom 缺少这些接口：%s' % ', '.join(missing))
    return _wxcom


def _key_of(args):
    if args.key_env:
        v = os.environ.get(args.key_env)
        if not v:
            raise SystemExit('环境变量 %s 是空的' % args.key_env)
        return bytes.fromhex(v.strip())
    if args.key_hex:
        return bytes.fromhex(args.key_hex.strip())
    raise SystemExit('必须给 --key-hex 或 --key-env（密钥是每个库一把，不能猜）')


# ------------------------------------------------------------------ BytesExtra
# 薄封装：真正的实现都在共享库 wxcom 里（`media_paths` / `rewrite` / `basename`）。
# 这里不另写一套 —— 免得同一个坑在两个地方各踩一次。
def subs_of(be):
    """外层字段3 的 {子类型: bytes}（路径类子类型都在这里）"""
    return _need('media_paths').media_paths(be)


def rewrite_subs(be, new_for):
    """重建 BytesExtra：只替换 new_for 点名的子类型的值，其余字段原顺序重发。"""
    return _need('rewrite').rewrite(be, new_for)


def basename(p):
    """按两种分隔符取文件名 —— WSL 下 os.path.basename 不认反斜杠（真踩过）"""
    return _need('basename').basename(p)


def index_dirs(dirs):
    """文件名 -> 源路径；先扫到的优先。"""
    idx = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                idx.setdefault(f, p)
    return idx


# ------------------------------------------------------------------ 通用管道
def paths_of(args):
    """-> `(root, acct_root, msg_db)`。

    **注意顺序**：第 1 个是数据根（`--wechat-dir`），第 2 个才是账号目录。
    这里曾经把第 1 个当账号目录用过 —— 文件被拷到少一层账号名的位置，
    而 BytesExtra 里记的是带账号名的相对路径，于是 verify 报「路径所指文件缺失」。
    兄弟脚本 `wxvoice.py` 的 `paths_of()` 返回的顺序**不同**（账号目录在最前），
    改的时候务必看清本函数的返回值。
    """
    root = os.path.abspath(os.path.expanduser(args.wechat_dir))
    acct_root = os.path.join(root, args.account)
    if not os.path.isdir(acct_root):
        raise SystemExit('账号目录不存在：%s（检查 --wechat-dir / --account）' % acct_root)
    msg_db = args.msg_db or os.path.join(acct_root, 'Msg', 'Multi', 'MSG0.db')
    return root, acct_root, msg_db


def key_verdict(plain, bad, npages):
    """判断"密钥对不对" —— **不要**看页 HMAC 失败总数。

    坑：客户端会把库**预分配**到固定大小（见过 52 MB / 62 MB），尾部那些空白页是裸零、
    根本不是合法 SQLCipher 页，解密时必然 HMAC 失败。所以「失败页数 > 0」**不等于**密钥错，
    真实数据是「头部页数之内 0 失败，预分配区几千页失败，密钥完全正确」。
    可靠判据是：明文页头是不是 SQLite magic ＋ 失败页是否都落在空白区。
    """
    hp = _need('header_pages').header_pages(plain)
    slack = max(0, npages - hp)
    with open(plain, 'rb') as f:
        magic_ok = f.read(16) == b'SQLite format 3\x00'
    if not magic_ok:
        v = '✗ 密钥不对：明文页头不是 SQLite magic'
    elif bad == 0:
        v = '✓ 正常（0 页失败）'
    elif bad <= slack + 4:
        v = '✓ 正常（%d 页失败都落在预分配空白区；空白共 %d 页）' % (bad, slack)
    else:
        v = '？可疑：头部页数之内也有失败页（失败 %d 页 > 空白 %d 页），先确认密钥' % (bad, slack)
    return hp, slack, v, magic_ok


def decrypt_inst(args, msg_db):
    """把已安装的 MSG*.db 解密到工作目录，返回 (明文路径, HMAC 不符页数, 页数)。"""
    _need('decrypt_db', 'read_salt', 'header_pages')
    os.makedirs(args.work, exist_ok=True)
    plain = os.path.join(args.work, '_plain_%s.db' % args.kind)
    b, bad, npages = _wxcom.decrypt_db(msg_db, _key_of(args))
    with open(plain, 'wb') as f:
        f.write(b)
    hp, slack, verdict, magic_ok = key_verdict(plain, bad, npages)
    print('解密 %s -> %s' % (msg_db, plain))
    print('  头部页数 %d / 文件页数 %d（预分配空白 %d 页）；页 HMAC 未通过 %d 页'
          % (hp, npages, slack, bad))
    print('  密钥判定：%s' % verdict)
    if not magic_ok:
        raise SystemExit('密钥不对，先别往下走（明文不是合法 SQLite 库）。')
    return plain, bad, npages


# ------------------------------------------------------------------ 计划
def plan_images(c, args, acct_root):
    idx = index_dirs([os.path.join(args.archive, 'Data'),
                      os.path.join(args.archive, 'Data', 'Tiny')])
    rows = []
    for lid, talker, ct, be in c.execute(
            'select localId,StrTalker,CreateTime,BytesExtra from MSG '
            'where Type=3 and BytesExtra is not null'):
        subs = subs_of(be)
        rec = {'lid': lid, 'talker': talker, 'ct': ct, 'new': {}, 'copy': {}, 'miss': []}
        if 4 not in subs:
            rec['nopath'] = True
            rows.append(rec)
            continue
        rec['cur'] = subs[4].decode('utf-8', 'replace')
        if b'FileStorage' in subs[4]:
            rec['done'] = True
            rows.append(rec)
            continue
        md5t = hashlib.md5(talker.encode()).hexdigest()
        ym = time.strftime('%Y-%m', time.localtime(ct))
        for st, kind in ((3, 'Thumb'), (4, 'Image')):
            p = subs.get(st)
            if not p:
                continue
            bn = basename(p.decode('utf-8', 'replace'))
            src = idx.get(bn)
            if not src:
                rec['miss'].append(bn)
                continue
            rel = '%s\\FileStorage\\MsgAttach\\%s\\%s\\%s\\%s' % (
                args.account, md5t, kind, ym, bn)
            rec['new'][st] = rel.encode()
            rec['copy'][st] = (src, os.path.join(acct_root, 'FileStorage', 'MsgAttach',
                                                 md5t, kind, ym, bn))
        rows.append(rec)
    return rows


def plan_video(c, args, acct_root):
    idx = index_dirs([os.path.join(args.archive, 'Video')])
    rows = []
    for lid, talker, ct, be in c.execute(
            'select localId,StrTalker,CreateTime,BytesExtra from MSG where Type in (43,62)'):
        subs = subs_of(be)
        rec = {'lid': lid, 'talker': talker, 'ct': ct, 'new': {}, 'copy': {}, 'miss': []}
        if not subs:
            rec['nopath'] = True
            rows.append(rec)
            continue
        rec['cur'] = (subs.get(4) or b'').decode('utf-8', 'replace')
        if 4 in subs and b'FileStorage' in subs[4]:
            rec['done'] = True
            rows.append(rec)
            continue
        for st in (3, 4):
            p = subs.get(st)
            if not p:
                continue
            bn = basename(p.decode('utf-8', 'replace'))
            src = idx.get(bn)
            if not src:
                rec['miss'].append(bn)
                continue
            rel = '%s\\FileStorage\\Video\\%s' % (args.account, bn)
            rec['new'][st] = rel.encode()
            # 视频层是扁平的：没有 <YYYY-MM> 子目录
            rec['copy'][st] = (src, os.path.join(acct_root, 'FileStorage', 'Video', bn))
        rows.append(rec)
    return rows


def sticker_names(c):
    """Type=47 的子类型4 是**裸的 32 位大写 hex 名字**（不是路径）"""
    need = {}
    for lid, be in c.execute('select localId,BytesExtra from MSG where Type=47'):
        p = subs_of(be).get(4)
        if not p:
            continue
        v = p.decode('utf-8', 'replace')
        if len(v) == 32 and v.isupper():
            need.setdefault(v, []).append(lid)
    return need


def plan_files(c, args, acct_root):
    roots = [os.path.join(args.archive, 'Attachment'),
             os.path.join(args.archive, 'Data'),
             os.path.join(acct_root, 'FileStorage', 'File')]
    roots += [r for r in (args.extra_root or [])]
    idx = index_dirs([r for r in roots if os.path.isdir(r)])
    rows = []
    for lid, talker, ct, sc, be in c.execute(
            "select localId,StrTalker,CreateTime,StrContent,BytesExtra from MSG "
            "where Type=49 and StrContent like '%<type>6</type>%'"):
        subs = subs_of(be)
        rec = {'lid': lid, 'talker': talker, 'ct': ct, 'new': {}, 'copy': {}, 'miss': []}
        m = re.search(r'<totallen>(\d+)</totallen>', sc or '')
        rec['totallen'] = int(m.group(1)) if m else None
        if 4 not in subs:
            rec['nopath'] = True
            rows.append(rec)
            continue
        p = subs[4].decode('utf-8', 'replace')
        rec['cur'] = p
        rec['bn'] = basename(p)
        if 'FileStorage' in p:
            rec['done'] = True
            rows.append(rec)
            continue
        src = idx.get(rec['bn'])
        if not src:
            rec['miss'].append(rec['bn'])
            rows.append(rec)
            continue
        ym = time.strftime('%Y-%m', time.localtime(ct))
        rel = '%s\\FileStorage\\File\\%s\\%s' % (args.account, ym, rec['bn'])
        rec['new'][4] = rel.encode()
        rec['copy'][4] = (src, os.path.join(acct_root, 'FileStorage', 'File', ym, rec['bn']))
        rows.append(rec)
    return rows


def make_plan(c, args, acct_root):
    if args.kind == 'stickers':
        return sticker_names(c)
    if args.kind == 'images':
        return plan_images(c, args, acct_root)
    if args.kind == 'video':
        return plan_video(c, args, acct_root)
    return plan_files(c, args, acct_root)


def copy_files(rows):
    """照计划复制（幂等：目标已存在且大小一致就跳过）"""
    copied = skipped = 0
    for rec in rows:
        for _st, (src, dst) in rec['copy'].items():
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
                skipped += 1
                continue
            shutil.copy2(src, dst)
            copied += 1
    return copied, skipped


def local_file_of(root, rec):
    """这一行对应的"现在就能核对的本地文件"。

    优先用**归档里的源文件**：`check` 时 build 还没跑，计划中的目标路径并不存在，
    拿它去 getsize 会直接抛 FileNotFoundError（冒烟测试就是这么抓出来的）。
    已经就位（`done`）的行则用 BytesExtra 里记录的路径。
    """
    if rec['copy']:
        src = list(rec['copy'].values())[0][0]
        if os.path.exists(src):
            return src
    if rec.get('cur') and rec.get('done'):
        cand = os.path.join(root, rec['cur'].replace('\\', '/'))
        if os.path.exists(cand):
            return cand
    return None


# ------------------------------------------------------------------ check
def cmd_check(args):
    root, acct_root, msg_db = paths_of(args)
    _need('integrity')
    plain, _bad, _n = decrypt_inst(args, msg_db)
    c = sqlite3.connect(plain)
    print('已安装库 integrity = %s' % _wxcom.integrity(plain))

    if args.kind == 'stickers':
        need = sticker_names(c)          # {名字: [localId, ...]}
        needset = set(need)
        dst_root = os.path.join(acct_root, 'FileStorage', 'CustomEmotion')
        have = set()
        for sub in (os.listdir(dst_root) if os.path.isdir(dst_root) else []):
            d = os.path.join(dst_root, sub)
            if os.path.isdir(d):
                have |= set(os.listdir(d))
        arch = index_dirs([os.path.join(args.archive, 'CustomEmotions')])
        add = sorted(needset & set(arch))
        print('\n=== 表情 (Type=47) ===')
        print('  库里需要的名字（去重）      : %d' % len(needset))
        print('  归档 CustomEmotions/ 的文件 : %d' % len(arch))
        print('  目标目录已有文件            : %d（其中命中需求 %d）'
              % (len(have), len(needset & have)))
        print('  可从归档补进去的            : %d' % len(add))
        print('  归档里也没有的              : %d' % len(needset - set(arch)))
        for name in add[:8]:
            print('    + %s' % name)
        print('\n  表情不改数据库，纯文件放置；客户端会嗅探格式，所以明文 GIF 可用。')
        c.close()
        return 0

    rows = make_plan(c, args, acct_root)
    done = [r for r in rows if r.get('done')]
    todo = [r for r in rows if not r.get('done') and not r.get('nopath')]
    nopath = [r for r in rows if r.get('nopath')]
    ok = [r for r in todo if r['new']]
    miss = [r for r in todo if not r['new']]
    print('\n=== %s ===' % args.kind)
    print('  库里共引用             : %d 行' % len(rows))
    print('  已指向 FileStorage     : %d 行（无需再动）' % len(done))
    print('  BytesExtra 里没有路径  : %d 行' % len(nopath))
    print('  本次可改               : %d 行' % len(ok))
    print('  归档里找不到源文件     : %d 行（%d 个文件名）' % (len(miss), sum(len(r['miss']) for r in rows)))
    for r in ok[:8]:
        print('    localId=%-7d %-24s %s' % (r['lid'], r['talker'], sorted(r['new'])))
    for r in miss[:5]:
        print('    缺源 localId=%-7d %s' % (r['lid'], ', '.join(r['miss'][:2])))

    if args.kind == 'files':
        mm = absent = 0
        samples = []
        for r in rows:
            f = local_file_of(root, r)
            if not f:
                if r.get('cur'):
                    absent += 1
                continue
            if r.get('totallen'):
                sz = os.path.getsize(f)
                if sz != r['totallen']:
                    mm += 1
                    samples.append((r['lid'], r.get('bn') or '', r['totallen'], sz))
        print('  <totallen> 与本地文件大小不一致: %d 行' % mm)
        print('    不一致往往意味着「同名不同物」——按文件名匹配会拿错文件，务必人工看几眼')
        for lid, bn, want, got in samples[:5]:
            print('      localId=%-7d %-40s 库中 %d B / 实际 %d B' % (lid, bn[:40], want, got))
        if absent:
            print('  已记录路径但本地找不到文件    : %d 行' % absent)
    c.close()
    return 0


# ------------------------------------------------------------------ build
def cmd_build(args):
    _need('integrity', 'encrypt_db', 'read_salt', 'truncate_to_header', 'set_journal_mode')
    _root, acct_root, msg_db = paths_of(args)      # 顺序见 paths_of() 的 docstring
    plain, _bad, _n = decrypt_inst(args, msg_db)
    c = sqlite3.connect(plain)

    if args.kind == 'stickers':
        need = sticker_names(c)
        arch = index_dirs([os.path.join(args.archive, 'CustomEmotions')])
        dst_root = os.path.join(acct_root, 'FileStorage', 'CustomEmotion')
        n = skip = 0
        for name in sorted(set(need) & set(arch)):
            d = os.path.join(dst_root, name[:2])
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, name)
            if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(arch[name]):
                skip += 1
                continue
            shutil.copy2(arch[name], dst)
            n += 1
        print('表情：放入 %d 个文件，跳过 %d 个（**不改数据库**）' % (n, skip))
        c.close()
        return 0

    rows = make_plan(c, args, acct_root)
    copied, skipped = copy_files(rows)
    fixed = 0
    for rec in rows:
        if not rec['new'] or rec.get('done'):
            continue
        be = c.execute('select BytesExtra from MSG where localId=?', (rec['lid'],)).fetchone()[0]
        c.execute('update MSG set BytesExtra=? where localId=?',
                  (rewrite_subs(be, rec['new']), rec['lid']))
        fixed += 1
    c.commit()
    print('复制文件 %d 个（跳过已存在 %d 个），改写 BytesExtra %d 行' % (copied, skipped, fixed))
    print('integrity=%s' % _wxcom.integrity(plain))
    c.close()

    out = os.path.join(args.work, 'MSG0_%s.db' % args.kind)
    # 日志模式必须显式设回 WAL(2/2)：`decrypt_db` 会把明文页头第 18/19 字节归一成 1/1，
    # 而 `encrypt_db` 不会替我们改回来 —— 漏了这一步就会**静默把客户端的 WAL 降级成
    # rollback**（3.x 自己写的库全是 `10 00 02 02`）。这两个字节在 page 0 的密文里，
    # 所以必须在加密**之前**、在明文上改。
    mode = _need('set_journal_mode').set_journal_mode(plain, wal=True)
    print('日志模式：%d/%d（WAL）' % (mode, mode))
    npg = _wxcom.truncate_to_header(plain)
    print('按头部页数截断：%d 页' % npg)
    npg = _wxcom.encrypt_db(plain, out, _key_of(args), _wxcom.read_salt(msg_db))
    print('加密输出 %s（%d 页，%d B）' % (out, npg, os.path.getsize(out)))
    return 0


# ------------------------------------------------------------------ verify
def cmd_verify(args):
    _need('decrypt_db', 'integrity', 'usable_diff')
    wx = _wxcom
    root, acct_root, _msg_db = paths_of(args)

    if args.kind == 'stickers':
        # 表情**不改数据库**，所以没有库产物可做 HMAC/往返校验；
        # 这里只确认「库里需要的名字」在磁盘上都在位。（早先这里仍去要求
        # MSG0_stickers.db 存在，于是 verify 永远 FAIL —— 冒烟测试抓出来的。）
        plain, _b, _n = decrypt_inst(args, _msg_db)
        c = sqlite3.connect(plain)
        need = sticker_names(c)
        c.close()
        dst_root = os.path.join(acct_root, 'FileStorage', 'CustomEmotion')
        missing = [n for n in need if not os.path.exists(os.path.join(dst_root, n[:2], n))]
        print('表情：库里需要 %d 个名字，磁盘上缺失 %d 个' % (len(need), len(missing)))
        for n in sorted(missing)[:8]:
            print('    缺 %s' % n)
        good = not missing
        print('VERIFY:', 'PASS' if good else 'FAIL')
        return 0 if good else 1

    out = os.path.join(args.work, 'MSG0_%s.db' % args.kind)
    plain = os.path.join(args.work, '_plain_%s.db' % args.kind)
    if not os.path.exists(out):
        raise SystemExit('先跑 build：找不到 %s' % out)
    b, bad, npages = wx.decrypt_db(out, _key_of(args))
    print('HMAC 不符页=%d/%d' % (bad, npages))
    tmp = out + '.roundtrip'
    good = False
    with open(tmp, 'wb') as f:
        f.write(b)
    try:
        # 重新加密必然换新 IV，所以只能比「可用区」；这一步由共享库负责
        diff = wx.usable_diff(tmp, plain)
        print('可用区不一致页=%d' % diff)
        print('往返库 integrity=%s' % wx.integrity(tmp))
        c = sqlite3.connect(tmp)
        rows = make_plan(c, args, acct_root)
        ok = miss = 0
        for rec in rows:
            if not rec.get('cur'):
                continue
            be = c.execute('select BytesExtra from MSG where localId=?',
                           (rec['lid'],)).fetchone()[0]
            for st, val in subs_of(be).items():
                if st not in (3, 4):
                    continue
                f = os.path.join(root, val.decode('utf-8', 'replace').replace('\\', '/'))
                if os.path.exists(f):
                    ok += 1
                else:
                    miss += 1
        print('路径所指文件：存在 %d，缺失 %d' % (ok, miss))
        good = bad == 0 and diff == 0 and miss == 0
        c.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    print('VERIFY:', 'PASS' if good else 'FAIL')
    return 0 if good else 1


# ------------------------------------------------------------------ install
def cmd_install(args):
    msg_db = paths_of(args)[2]
    if args.kind == 'stickers':
        print('表情是纯文件放置，**没有数据库产物需要安装** —— 文件在 build 时已就位。')
        print('（改得越少，回退越容易。）')
        return 0
    if cmd_verify(args) != 0:
        raise SystemExit('校验未通过，拒绝安装')
    out = os.path.join(args.work, 'MSG0_%s.db' % args.kind)
    # 共享库的守卫式安装：目标旁边若有非零 -wal 就拒绝（客户端没干净退出），
    # 先备份原库（含 -wal/-shm），并打印还原命令。
    _need('backup_and_install').backup_and_install(
        out, msg_db, args.backup_dir or os.path.join(args.work, 'pre_install'),
        label='MSG')
    print('\n完成。启动客户端后确认：媒体不再是「已被清理」。')
    return 0


# ------------------------------------------------------------------ CLI
KINDS = ('images', 'video', 'stickers', 'files')
ACTIONS = {'check': cmd_check, 'build': cmd_build,
           'verify': cmd_verify, 'install': cmd_install}


def build_parser(kind):
    p = argparse.ArgumentParser(
        prog='wxmedia.py %s' % kind,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=USAGE)
    p.add_argument('action', nargs='?', default='check',
                   choices=['check', 'build', 'verify', 'install'])
    p.add_argument('--wechat-dir', required=True, help='微信数据根（其下有 <账号> 目录）')
    p.add_argument('--account', required=True, help='账号目录名')
    p.add_argument('--archive', required=True, help='2.x 归档的账号目录（媒体来源）')
    p.add_argument('--extra-root', action='append', default=[],
                   help='额外的文件搜索根（可重复；仅 files 用）')
    p.add_argument('--msg-db', default='', help='显式指定 MSG*.db')
    p.add_argument('--key-hex', default='', help='MSG*.db 的 AES 密钥（hex）')
    p.add_argument('--key-env', default='', help='从该环境变量读密钥 hex（优先）')
    p.add_argument('--work', default='./_wxwork', help='工作目录（默认 ./_wxwork）')
    p.add_argument('--backup-dir', default='', help='install 的备份目录')
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    kind = argv[0]
    if kind not in KINDS:
        print('未知类型 %r；可选：%s' % (kind, ' | '.join(KINDS)))
        print(USAGE)
        return 2
    args = build_parser(kind).parse_args(argv[1:])   # --help 在这里也会退出 0
    args.kind = kind
    return ACTIONS[args.action](args) or 0


if __name__ == '__main__':
    sys.exit(main())
