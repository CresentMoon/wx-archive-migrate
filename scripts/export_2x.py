#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把**已解密**的 2.x 库导出成人能读的聊天记录（日志 / 分会话 txt / CSV）。

输入是 `wxdec.py` 解出来的明文副本（或直接从明文副本拷来的一目录 `*.db`），
本脚本**只读**它们，不改动、不删除。

用法：
    python3 export_2x.py --plain ./WeChat_2x_plain --out ./WeChat_2x_recovered \\
        --self <你自己的账号目录名>

    python3 export_2x.py --selftest        # 用虚构数据端到端跑一遍

环境变量（对应参数未给出时作为兜底）：
    WX_PLAIN_DIR   明文 `*.db` 所在目录（默认 WeChat_2x_plain）
    WX_OUT_DIR     输出目录（默认 WeChat_2x_recovered）
    WX_SELF        本机账号的目录名，只用于日志表头（默认 MyAccount）

输出（都在 `--out` 下）：
    README.md、聊天记录_全部.txt、聊天记录_按会话/<序号>_<名称>.txt、
    消息.csv、联系人.csv、00_解密数据库/*.db

退出码：0 = 成功；1 = 输入不完整或库结构不符；2 = 参数错误。
"""
import argparse
import csv
import datetime
import os
import re
import shutil
import sqlite3
import sys
import tempfile

DEFAULT_PLAIN = 'WeChat_2x_plain'
DEFAULT_OUT = 'WeChat_2x_recovered'
DEFAULT_SELF = 'MyAccount'

TYPE_NAMES = {
    1: '', 3: '[图片]', 34: '[语音]', 37: '[好友请求]', 40: '[POSSIBLE FRIEND]',
    42: '[名片]', 43: '[视频]', 47: '[表情]', 48: '[位置]', 49: '[链接/文件]',
    50: '[语音通话]', 62: '[小视频]', 10000: '[系统]',
}

SEP = '=' * 78


def appmsg_summary(xml):
    def pick(tag):
        m = re.search(r'<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>' % (tag, tag), xml, re.S)
        return (m.group(1).strip() if m else '')
    t, d, u = pick('title'), pick('des'), pick('url')
    inner = re.search(r'<type>\s*(\d+)', xml)
    kind = inner.group(1) if inner else '?'
    label = {'5': '链接', '6': '文件', '57': '引用', '33': '小程序', '36': '小程序',
             '2000': '转账', '17': '实时位置', '19': '聊天记录'}.get(kind, 'appmsg' + kind)
    parts = ['[%s]' % label]
    if t:
        parts.append(t)
    if d and d != t:
        parts.append('— ' + d)
    if u:
        parts.append(u)
    return ' '.join(parts)


def contact_names(plain):
    """UserName -> (最好看的显示名, UserName)。"""
    names = {}
    c = sqlite3.connect(os.path.join(plain, 'MicroMsg.db'))
    try:
        for un, alias, remark, nick in c.execute(
                'select UserName, Alias, Remark, NickName from Contact'):
            best = remark or nick or alias or un
            names[un] = (best or un, un)
    finally:
        c.close()
    return names


def chatroom_members(plain):
    """room -> {wxid: 群昵称}"""
    c = sqlite3.connect(os.path.join(plain, 'MicroMsg.db'))
    rooms = {}
    try:
        for rn, ul, dl in c.execute(
                'select ChatRoomName, UserNameList, DisplayNameList from ChatRoom'):
            users = (ul or '').split('^G')
            disps = (dl or '').split('^G')
            m = {}
            for i, u in enumerate(users):
                if not u:
                    continue
                d = disps[i] if i < len(disps) else ''
                m[u] = d or ''
            rooms[rn] = m
    finally:
        c.close()
    return rooms


def strings_in_blob(b):
    return [s.decode('utf-8', 'ignore')
            for s in re.findall(rb'[\x20-\x7e]{4,64}', b or b'')]


def group_sender(row, names, room_members):
    """群消息：从 bytesExtra / 正文里把发送者挖出来。"""
    talker = row['strTalker']
    if not talker.endswith('@chatroom') or row['IsSender']:
        return None
    xml = row['strContent'] or ''
    m = re.search(r'<fromusername>(?:<!\[CDATA\[)?([^\]<]+)', xml)
    if m:
        return m.group(1).strip()
    known = room_members.get(talker, {})
    for s in strings_in_blob(row['bytesExtra']):
        if s in known or s in names:
            return s
    return None


def render(row, names, room_members):
    t = row['type']
    talker = row['strTalker']
    if talker.endswith('@chatroom'):
        if row['IsSender']:
            who = '我'
        else:
            s = group_sender(row, names, room_members)
            if s:
                who = room_members.get(talker, {}).get(s) or names.get(s, (s, s))[0]
            else:
                who = '（群成员）'
    else:
        who = '我' if row['IsSender'] else names.get(talker, (talker, talker))[0]
    body = row['strContent'] or ''
    if t == 49:
        body = appmsg_summary(body)
    elif t == 10000:
        body = re.sub(r'^[\s\S]{0,60}?"?\s*', '', body, count=1).strip() or body
        body = re.sub(r'<[^>]+>', '', body)
    elif t in (3, 43, 62):
        md5 = re.search(r'md5="([0-9a-f]{32})"', body)
        body = TYPE_NAMES[t] + ('  md5=%s' % md5.group(1) if md5 else '')
    elif t == 47:
        md5 = re.search(r'md5="([0-9a-fA-F]{32})"', body)
        body = TYPE_NAMES[t] + ('  md5=%s' % md5.group(1) if md5 else '')
    elif t == 34:
        body = TYPE_NAMES[t]
    elif t in (42, 48, 50, 37, 40):
        body = TYPE_NAMES[t]
    elif TYPE_NAMES.get(t):
        body = (TYPE_NAMES[t] + ' ' + body).strip()
    body = re.sub(r'\s*\n\s*', ' / ', body).strip()
    return who, body


def fname(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', s).strip() or 'unknown'
    return s[:60]


def export(plain, out, self_id):
    if not os.path.isdir(plain):
        raise SystemExit('输入目录不存在：%s（用 --plain 指定明文库所在目录）' % plain)
    if not os.path.exists(os.path.join(plain, 'ChatMsg.db')):
        raise SystemExit('%s 下没有 ChatMsg.db —— 这里放的应当是**明文**副本' % plain)

    names = contact_names(plain)
    rooms = chatroom_members(plain)
    os.makedirs(out, exist_ok=True)
    dbdir = os.path.join(out, '00_解密数据库')
    os.makedirs(dbdir, exist_ok=True)
    for f in sorted(os.listdir(plain)):
        if f.endswith('.db'):
            shutil.copy2(os.path.join(plain, f), os.path.join(dbdir, f))

    c = sqlite3.connect(os.path.join(plain, 'ChatMsg.db'))
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            'select * from ChatMsg order by CreateTime, localId').fetchall()
    finally:
        c.close()

    per = {}
    csvrows = []
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r['CreateTime']).strftime('%Y-%m-%d %H:%M:%S')
        who, body = render(r, names, rooms)
        per.setdefault(r['strTalker'], []).append('%s  %s: %s' % (ts, who, body))
        csvrows.append([r['localId'], ts, r['CreateTime'], r['strTalker'],
                        names.get(r['strTalker'], (r['strTalker'],))[0],
                        r['type'], r['IsSender'], who, body])

    with open(os.path.join(out, '聊天记录_全部.txt'), 'w', encoding='utf-8') as f:
        f.write('微信聊天记录导出（本机账号 %s）\n' % self_id)
        if rows:  # 空表也要能导出，不能在这里炸
            f.write('共 %d 条，%s → %s\n' % (
                len(rows),
                datetime.datetime.fromtimestamp(rows[0]['CreateTime']),
                datetime.datetime.fromtimestamp(rows[-1]['CreateTime'])))
        else:
            f.write('共 0 条（ChatMsg 是空表）\n')
        f.write(SEP + '\n\n')
        for r in rows:
            ts = datetime.datetime.fromtimestamp(r['CreateTime']).strftime('%Y-%m-%d %H:%M:%S')
            who, body = render(r, names, rooms)
            f.write('%s  [%s]  %s: %s\n' % (ts, r['strTalker'], who, body))

    d = os.path.join(out, '聊天记录_按会话')
    os.makedirs(d, exist_ok=True)
    order = sorted(per.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for i, (talker, lines) in enumerate(order, 1):
        title = names.get(talker, (talker,))[0]
        if talker.endswith('@chatroom'):
            title = '群_' + title
        fn = '%03d_%s_%d条.txt' % (i, fname(title), len(lines))
        with open(os.path.join(d, fn), 'w', encoding='utf-8') as f:
            f.write('# %s  (%s)  %d 条\n\n' % (title, talker, len(lines)))
            f.write('\n'.join(lines) + '\n')

    with open(os.path.join(out, '消息.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['localId', '时间', 'CreateTime', 'strTalker', '会话名',
                    'type', 'IsSender', '发送者', '内容'])
        w.writerows(csvrows)

    with open(os.path.join(out, '联系人.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['UserName', '备注/昵称', 'Alias', 'NickName', 'Remark', 'Type'])
        c = sqlite3.connect(os.path.join(plain, 'MicroMsg.db'))
        try:
            for r in c.execute('select UserName, Alias, NickName, Remark, Type '
                               'from Contact order by NickName'):
                w.writerow([r[0], names.get(r[0], ('',))[0], r[1], r[2], r[3], r[4]])
        finally:
            c.close()

    with open(os.path.join(out, 'README.md'), 'w', encoding='utf-8') as f:
        f.write('# 微信聊天记录导出\n\n')
        f.write('由 `scripts/export_2x.py` 生成；本机账号：`%s`。\n\n' % self_id)
        f.write('| 文件 | 说明 |\n|---|---|\n')
        f.write('| `聊天记录_全部.txt` | 全部消息，按时间排序；'
                '每行 `时间 [会话] 发送者: 内容` |\n')
        f.write('| `聊天记录_按会话/` | 每个会话一个 txt，按消息条数从多到少编号 |\n')
        f.write('| `消息.csv` | 一行一条消息，含 `localId` / `CreateTime` /'
                ' 原始 `type` / `IsSender` |\n')
        f.write('| `联系人.csv` | 联系人表（UserName / 备注 / Alias / 昵称 / 类型） |\n')
        f.write('| `00_解密数据库/` | 输入明文库的原样副本 |\n\n')
        f.write('> 本目录含**真实会话名与聊天内容**，属个人数据：'
                '不要提交进任何公开仓库，也不要用网盘公开分享。\n')

    return {'conversations': len(per), 'messages': len(rows), 'out': out}


CHATMSG_SCHEMA = """
create table ChatMsg (
  localId integer primary key, CreateTime integer, strTalker text,
  IsSender integer, type integer, strContent text, bytesExtra blob)
"""
MICROMSG_SCHEMA = """
create table Contact (UserName text, Alias text, NickName text, Remark text, Type integer);
create table ChatRoom (ChatRoomName text, UserNameList text, DisplayNameList text)
"""


def selftest():
    """用虚构数据端到端跑一遍 export()，并核对产物。不需要任何真实归档。"""
    base = datetime.datetime(2001, 1, 2, 3, 4, 5)
    t = [int(base.timestamp()) + i * 60 for i in range(4)]
    with tempfile.TemporaryDirectory() as d:
        plain = os.path.join(d, 'plain')
        out = os.path.join(d, 'out')
        os.makedirs(plain)
        m = sqlite3.connect(os.path.join(plain, 'MicroMsg.db'))
        m.executescript(MICROMSG_SCHEMA)
        m.executemany('insert into Contact values (?,?,?,?,?)', [
            ('user_a', 'alias_a', '昵称A', '备注A', 3),
            ('user_b', 'alias_b', '昵称B', '', 3),
        ])
        m.execute('insert into ChatRoom values (?,?,?)',
                  ('room_c@chatroom', 'user_a^Guser_b', '甲^G乙'))
        m.commit()
        m.close()

        c = sqlite3.connect(os.path.join(plain, 'ChatMsg.db'))
        c.executescript(CHATMSG_SCHEMA)
        c.executemany(
            'insert into ChatMsg (localId, CreateTime, strTalker, IsSender, type,'
            ' strContent, bytesExtra) values (?,?,?,?,?,?,?)', [
                (1, t[0], 'user_a', 1, 1, '单聊文本', b''),
                (2, t[1], 'user_a', 0, 3, '<msg><img md5="' + 'a' * 32 + '"/></msg>', b''),
                (3, t[2], 'room_c@chatroom', 0, 1,
                 '<msgsource><fromusername>user_b</fromusername></msgsource>群消息', b''),
                (4, t[3], 'room_c@chatroom', 1, 1, '我也在群里说', b''),
            ])
        c.commit()
        c.close()

        r = export(plain, out, 'selftest')
        want = ['README.md', '聊天记录_全部.txt', '消息.csv', '联系人.csv',
                os.path.join('聊天记录_按会话'), os.path.join('00_解密数据库')]
        for w in want:
            if not os.path.exists(os.path.join(out, w)):
                print('SELFTEST FAILED: 缺少产物 %s' % w)
                return 1
        if r['messages'] != 4 or r['conversations'] != 2:
            print('SELFTEST FAILED: 统计 %r（期望 4 条 / 2 个会话）' % r)
            return 1
        with open(os.path.join(out, '聊天记录_全部.txt'), encoding='utf-8') as f:
            log = f.read()
        for needle in ['本机账号 selftest', '备注A', '乙', '我也在群里说']:
            if needle not in log:
                print('SELFTEST FAILED: 日志里找不到 %r' % needle)
                return 1
        # 群消息必须归到群会话、单聊归到单聊：共 2 个会话文件
        d2 = os.path.join(out, '聊天记录_按会话')
        files = [f for f in os.listdir(d2) if f.endswith('.txt')]
        if len(files) != 2:
            print('SELFTEST FAILED: 分会话文件 %d 个（期望 2）：%s' % (len(files), files))
            return 1
        if not os.path.exists(os.path.join(out, '00_解密数据库', 'ChatMsg.db')):
            print('SELFTEST FAILED: 00_解密数据库 里没有拷进原库')
            return 1
        print('SELFTEST PASSED（4 条消息 / 2 个会话端到端导出，产物齐全）')
        return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog='export_2x.py',
        description='把已解密的 2.x 库导出成可读聊天记录（日志 / 分会话 txt / CSV）。',
        epilog='本脚本只读输入目录，不改动也不删除其中的任何文件。',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--plain', metavar='DIR', default=os.environ.get('WX_PLAIN_DIR', DEFAULT_PLAIN),
                   help='明文 *.db 所在目录（默认 %(default)s；环境变量 WX_PLAIN_DIR）')
    p.add_argument('--out', metavar='DIR', default=os.environ.get('WX_OUT_DIR', DEFAULT_OUT),
                   help='输出目录（默认 %(default)s；环境变量 WX_OUT_DIR）')
    p.add_argument('--self', dest='self_id', metavar='NAME',
                   default=os.environ.get('WX_SELF', DEFAULT_SELF),
                   help='本机账号的目录名，只用于日志表头（默认 %(default)s；环境变量 WX_SELF）')
    p.add_argument('--selftest', action='store_true',
                   help='用虚构数据端到端跑一遍后退出')
    return p


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    r = export(args.plain, args.out, args.self_id)
    print('conversations : %d' % r['conversations'])
    print('messages      : %d' % r['messages'])
    print('output        : %s' % r['out'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
