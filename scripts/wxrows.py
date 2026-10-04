#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxrows —— 把 2.x 的 `ChatMsg.db` 迁移成 3.x 的 `MSG0.db` 明文（并可选加密）。

这是整条链路里最容易做错的一步。列映射本身不难，难在**几个值的语义**，
下面每一条都有实测依据（全部来自「让真客户端自己写一行、再读它的列值」）。

## 规则（实测确认，约 3.2 万行零例外）

    Sequence      = CreateTime * 1000          ← 毫秒时间戳，**不是**每会话计数器
    MsgSequence   = 每会话自增计数器，从 1 开始（按 CreateTime, localId 排序）
    MsgServerSeq  = 1
    SubType       = 0
    StatusEx      = 0
    FlagEx        = 0
    Status        = 取源库的值（客户端自己写的行是 2）
    Reserved1     = 2 当 Type == 34（语音），否则 1
    Reserved0     = 0
    Reserved2..6  = NULL
    Name2ID       = 原样搬运，**rowid 就是 TalkerId**
    MSGTrans      = 源库 TransTable（表可能不存在，跳过并报告）
    sqlite_sequence = max(localId)

### 为什么 `Sequence` 这么要命

客户端的会话分页走索引 `MsgTalkerIdSeqIndex(talkerId, sequence DESC)`。
如果把 `Sequence` 填成「每会话计数器」，这些行的 `CreateTime` 是会话里最新的、
按 `Sequence` 排序却排到最前 —— 一个键走游标、另一个键找最新，两个键互相矛盾，
游标推不动。症状极具误导性：

  * 左侧会话列表日期**正确**（那是 `Session.nTime` 管的）；
  * 点进去**只显示几十条**；
  * 用「消息记录浏览（日期）」看个人会话会**卡死** —— Windows 事件
    **1002 Application Hang**（不是 1000，没有 crash dump），因为那是死循环不是非法访问。

## 目标表结构从哪来

`--schema docs/detail/schema-3x.sql`（必需）。但**不能直接把 DDL 灌进一个空文件**：
SQLCipher 的页布局是「可用区 4048 + 保留段 48」，而 SQLite 核心只会把保留段写成 0，
事后改这个字节会让每页的单元指针数组错位 48 字节、库立刻损坏（实测）。
所以 `build` 先用 `wxcom.make_sqlite_shell()` 造一个「1 页、0 表、保留段 48」的空壳，
再在里面执行 DDL —— 之后建表与插行都会按 4048 可用字节布局。

## `DBInfo` 两行种子（这才是语音能不能播的开关）

    (1, <START_TIME_MS>, 'Start Time')
    (2, 1,               'Prefix LocalId Index')

`Start Time` 是「本分片纪元」：客户端只对 `Sequence >= StartTime` 的消息去查本地媒体；
早于它的当成导入的历史，**直接跳过本地媒体去服务器下载** —— 而当年的 CDN 早已失效，
于是气泡上出现红色感叹号或无限转圈。所以默认把它设为**源数据最早一条消息的
`Sequence`**（`--start-time` 可覆盖）。副作用：这会让客户端滚出一个新分片
（`MSG1.db` / `MediaMSG1.db` / `FTSMSG1.db`），是预期行为。

动作：

    check    只读：源库/DDL 是否可用、将写入多少行、各列取值分布、Start Time 取值
    build    造 3.x 明文库；给了密钥与 --salt-from 时顺带加密
    verify   行数/时间跨度/会话数一致、Sequence 规则、TalkerId 可解析、HMAC、可用区往返
    install  写回客户端（--install-to）：先备份、打印还原命令

用法示例：

    export WX_KEY=<AES_KEY>
    python3 wxrows.py check  --src <ChatMsg.db> --schema docs/detail/schema-3x.sql
    python3 wxrows.py build  --src <ChatMsg.db> --schema docs/detail/schema-3x.sql \\
            --key-env WX_KEY --salt-from <MSG_DB>
    python3 wxrows.py verify --src <ChatMsg.db> --key-env WX_KEY --salt-from <MSG_DB>
    python3 wxrows.py install --install-to <MSG_DB> --src <ChatMsg.db> \\
            --key-env WX_KEY --salt-from <MSG_DB>
"""
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcom                                                     # noqa: E402

REQUIRED_TABLES = ('DBInfo', 'MSG', 'MSGTrans', 'Name2ID')
START_TIME_INDEX = 1
PREFIX_INDEX = 2
PREFIX_VERSION = 1

MSG_COLS = ('localId,TalkerId,MsgSvrID,Type,SubType,IsSender,CreateTime,Sequence,'
            'StatusEx,FlagEx,Status,MsgServerSeq,MsgSequence,StrTalker,StrContent,'
            'DisplayContent,Reserved0,Reserved1,Reserved2,Reserved3,Reserved4,'
            'Reserved5,Reserved6,CompressContent,BytesExtra,BytesTrans')

# 源库必需列 / 可选列（都按小写匹配；实测 2.x ChatMsg 的列名如下）
SRC_REQUIRED = ('localid', 'talkerid', 'createtime', 'strtalker', 'strcontent')
SRC_OPTIONAL = ('msgsvrid', 'type', 'issender', 'status', 'bytestrans', 'bytesextra')


# ------------------------------------------------------------------ 参数
def build_args(ap):
    ap.add_argument('--src', metavar='<ChatMsg.db>', help='2.x 明文源库（必需）')
    ap.add_argument('--src-table', default='ChatMsg', help='源表名（默认 ChatMsg）')
    ap.add_argument('--schema', metavar='<FILE>',
                    help='3.x 的 DDL（必需，例如 docs/detail/schema-3x.sql）')
    ap.add_argument('--out', metavar='<FILE>', help='明文输出（默认 <work>/MSG0.plain.db）')
    ap.add_argument('--enc-out', metavar='<FILE>', help='加密输出（默认 <work>/MSG0.db）')
    ap.add_argument('--install-to', metavar='<DB>', help='install 的目标：客户端里那个 MSG0.db')
    ap.add_argument('--start-time', metavar='<MS>', type=int,
                    help='DBInfo.Start Time（毫秒）；默认 = 源数据最早的 Sequence')
    ap.add_argument('--force', action='store_true',
                    help='install 时忽略「旁边有未检查点的 -wal」这一条告警')
    wxcom.add_common_args(ap)


# ------------------------------------------------------------------ 源库
def source_cols(conn, table):
    have = {r[1].lower(): r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)}
    if not have:
        raise SystemExit('源库里没有表 %s（用 --src-table 指定正确的表名）' % table)
    missing = [c for c in SRC_REQUIRED if c not in have]
    if missing:
        raise SystemExit('源表 %s 缺少必需列：%s\n  现有列：%s'
                         % (table, ', '.join(missing), ', '.join(sorted(have.values()))))
    return have


def read_source(path, table):
    if not os.path.exists(path):
        raise SystemExit('源库不存在：%s' % path)
    conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    have = source_cols(conn, table)
    want = list(SRC_REQUIRED) + [c for c in SRC_OPTIONAL if c in have]
    sel = ', '.join('"%s"' % have[c] for c in want)
    rows = list(conn.execute('select %s from "%s" order by CreateTime, localId'
                             % (sel, table)))
    trans = []
    try:
        trans = list(conn.execute('select msgLocalId, talkerId from TransTable'))
    except sqlite3.Error:
        pass
    name2id = list(conn.execute('select rowid, UsrName from Name2ID order by rowid'))
    conn.close()
    idx = {c: i for i, c in enumerate(want)}
    return {'rows': rows, 'idx': idx, 'want': want, 'trans': trans,
            'name2id': name2id, 'have': have}


def map_source_row(raw, idx):
    """把源行读成一个 dict；缺列给 None。"""
    def g(name):
        i = idx.get(name)
        return raw[i] if i is not None else None
    return {
        'localId': g('localid'),
        'TalkerId': g('talkerid'),
        'MsgSvrID': g('msgsvrid'),
        'Type': g('type'),
        'IsSender': g('issender'),
        'CreateTime': g('createtime'),
        'StrTalker': g('strtalker'),
        'StrContent': g('strcontent'),
        'Status': g('status'),
        'BytesTrans': g('bytestrans'),
        'BytesExtra': g('bytesextra'),
    }


# ------------------------------------------------------------------ DDL
def load_schema(path, section='A'):
    """读 3.x 的 DDL。

    仓库里那份参考 DDL 把**三个库**（MSG0 / MediaMSG0 / Media.db）写在同一个文件里，
    而 SQLite 的索引名是**库级全局且大小写不敏感** —— MSGTrans 上的 `talkerIDIdx`
    与 MediaInfo 上的 `TalkerIdIdx` 其实是同一个名字，整份一起执行必然报
    `index TalkerIdIdx already exists`。

    所以这里只取本库那一段（参考文件里用 `-- A.` / `-- B.` / `-- C.` 标记分段）。
    如果给的是一份单库 DDL（没有分段标记），就整份用它。
    """
    if not path:
        raise SystemExit('必须给 --schema <FILE>（3.x 的 DDL，例如 docs/detail/schema-3x.sql）')
    if not os.path.exists(path):
        raise SystemExit(
            '找不到 DDL 文件：%s\n'
            '  这一步必须显式指定目标表结构；仓库里预期有一份 docs/detail/schema-3x.sql，\n'
            '  内容至少要有这四张表：%s。' % (path, ', '.join(REQUIRED_TABLES)))
    sql = open(path, 'r', encoding='utf-8').read()
    if not sql.strip():
        raise SystemExit('DDL 文件是空的：%s' % path)
    marks = list(re.finditer(r'^-- ([ABC])\. [^\n]*\n', sql, re.M))
    if not marks:
        return sql
    for i, m in enumerate(marks):
        if m.group(1) != section:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(sql)
        got = sql[m.end():end]
        print('  取 %s 段（%d 字节）—— 该文件把三个库的 DDL 写在一起，只取本库那段'
              % (section, len(got)))
        return got
    raise SystemExit('DDL 文件里找不到 %s 段：%s（分段标记形如 `-- A. ...`）' % (section, path))


# ------------------------------------------------------------------ check
def cmd_check(args):
    print('=== 源库 ===')
    src = read_source(args.src, args.src_table)
    rows = src['rows']
    print('  %s  表 %s  行数 %d' % (args.src, args.src_table, len(rows)))
    print('  可用列: %s' % ', '.join(src['want']))
    miss = [c for c in SRC_OPTIONAL if c not in src['have']]
    if miss:
        print('  缺失的可选列（会补默认值）: %s' % ', '.join(miss))
    if not rows:
        raise SystemExit('源库里一行都没有，没什么可迁移的')

    spans = [map_source_row(r, src['idx']) for r in rows]
    cts = [m['CreateTime'] for m in spans if m['CreateTime'] is not None]
    talkers = {m['TalkerId'] for m in spans}
    print('  时间跨度: %d .. %d' % (min(cts), max(cts)))
    print('  会话数  : %d' % len(talkers))
    print('  Name2ID : %d 行   TransTable: %d 行'
          % (len(src['name2id']), len(src['trans'])))

    print()
    print('=== 类型分布（Reserved1 规则：Type=34 语音为 2，其余 1）===')
    dist = {}
    for m in spans:
        dist[m['Type']] = dist.get(m['Type'], 0) + 1
    for t in sorted(dist, key=lambda x: (x is None, x)):
        print('  Type=%-6s %6d 行   Reserved1=%d'
              % (t, dist[t], 2 if t == 34 else 1))

    print()
    print('=== Status 取值（源库原值；客户端自己写的行是 2）===')
    sd = {}
    for m in spans:
        sd[m['Status']] = sd.get(m['Status'], 0) + 1
    for st in sorted(sd, key=lambda x: (x is None, x)):
        note = '  <<< NULL，将按客户端常态补 2' if st is None else ''
        print('  Status=%-6s %6d 行%s' % (st, sd[st], note))

    print()
    print('=== 将要写入的 DBInfo（决定语音能不能播）===')
    st = args.start_time if args.start_time is not None else min(cts) * 1000
    print('  (1, %d, \'Start Time\')            <- 取源数据最早的 Sequence' % st)
    print('  (2, %d, \'Prefix LocalId Index\')' % PREFIX_VERSION)
    if st > min(cts) * 1000:
        print('  ⚠ 你指定的 Start Time 大于最早的 Sequence —— 早于它的消息会被客户端'
              '当成导入的历史而跳过本地媒体（语音会转圈）。')

    print()
    print('=== 目标 DDL ===')
    sql = load_schema(args.schema)
    print('  %s  %d 字节' % (args.schema, len(sql)))
    shell = sqlite3.connect(':memory:')
    try:
        shell.executescript(sql)
        have = {r[0] for r in shell.execute(
            "select name from sqlite_master where type='table'")}
    finally:
        shell.close()
    bad = [t for t in REQUIRED_TABLES if t not in have]
    if bad:
        raise SystemExit('DDL 缺少必需的表：%s（现有：%s）'
                         % (', '.join(bad), ', '.join(sorted(have))))
    print('  必需表齐全: %s' % ', '.join(REQUIRED_TABLES))
    extra = sorted(t for t in have - set(REQUIRED_TABLES) if not t.startswith('sqlite_'))
    if extra:
        print('  额外表（也会被建出来，可能来自别的分片）: %s' % ', '.join(extra))

    print()
    print('=== 一致性预检 ===')
    n2 = {r[0]: r[1] for r in src['name2id']}
    orph = [(m['localId'], m['TalkerId'], m['StrTalker']) for m in spans
            if m['TalkerId'] not in n2 or n2.get(m['TalkerId']) != m['StrTalker']]
    if orph:
        print('  ✗ %d 行的 TalkerId 与 StrTalker 对不上 Name2ID：' % len(orph))
        for x in orph[:10]:
            print('      localId=%s TalkerId=%s StrTalker=%s' % x)
        print('  （build 会因此拒绝执行 —— 客户端的会话分页靠 TalkerId，'
              '对不上会导致会话错乱）')
    else:
        print('  ✓ %d 行的 TalkerId 都能在 Name2ID 里解析且与 StrTalker 一致' % len(spans))
    print()
    print('（check 只读，没有写任何文件）')
    return 0


# ------------------------------------------------------------------ build
def build_rows(spans, start_time):
    """按规则生成 3.x 的 MSG 行。返回 (batch, 统计)。"""
    seq_counter = {}
    batch = []
    null_status = 0
    for m in spans:
        tid = m['TalkerId']
        seq_counter[tid] = seq_counter.get(tid, 0) + 1
        typ = m['Type'] or 0
        status = m['Status']
        if status is None:
            status = 2                       # 客户端自己写的行是 2
            null_status += 1
        batch.append((
            m['localId'], tid, m['MsgSvrID'] or 0, typ,
            0,                                   # SubType
            m['IsSender'] or 0, m['CreateTime'],
            m['CreateTime'] * 1000,              # Sequence = CreateTime*1000（毫秒）
            0,                                   # StatusEx
            0,                                   # FlagEx
            status,
            1,                                   # MsgServerSeq
            seq_counter[tid],                    # MsgSequence = 每会话计数器
            m['StrTalker'], m['StrContent'],
            None,                                # DisplayContent
            0,                                   # Reserved0
            2 if typ == 34 else 1,               # Reserved1
            None, None, None, None, None,        # Reserved2..6
            None,                                # CompressContent
            m['BytesExtra'], m['BytesTrans'],
        ))
    return batch, {'null_status': null_status, 'talkers': len(seq_counter)}


def cmd_build(args):
    out = args.out or os.path.join(args.work, 'MSG0.plain.db')
    enc_out = args.enc_out or os.path.join(args.work, 'MSG0.db')
    sql = load_schema(args.schema)
    src = read_source(args.src, args.src_table)
    spans = [map_source_row(r, src['idx']) for r in src['rows']]
    if not spans:
        raise SystemExit('源库没有数据行')
    cts = [m['CreateTime'] for m in spans if m['CreateTime'] is not None]
    if len(cts) != len(spans):
        raise SystemExit('有 %d 行的 CreateTime 是 NULL —— Sequence 无法成立，先修源库'
                         % (len(spans) - len(cts)))
    start_time = args.start_time if args.start_time is not None else min(cts) * 1000

    # 1) 先用「空壳」起手，让 SQLite 一开库就按保留段 48 布局
    os.makedirs(os.path.dirname(os.path.abspath(out)) or '.', exist_ok=True)
    if os.path.exists(out):
        os.remove(out)
    wxcom.make_sqlite_shell(out)
    print('空壳    : %s（1 页，0 表，保留段 %d；见 wxcom.make_sqlite_shell 的说明）'
          % (out, wxcom.RESERVE))

    conn = sqlite3.connect(out)
    conn.execute('PRAGMA page_size=%d' % wxcom.PAGE)
    conn.executescript(sql)
    have = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    bad = [t for t in REQUIRED_TABLES if t not in have]
    if bad:
        raise SystemExit('DDL 缺少必需的表：%s' % ', '.join(bad))

    # 2) Name2ID —— rowid 就是 TalkerId
    conn.executemany('insert into Name2ID(rowid, UsrName) values(?,?)', src['name2id'])
    print('Name2ID : %d 行' % len(src['name2id']))

    # 3) MSG
    batch, stat = build_rows(spans, start_time)
    conn.executemany('insert into MSG(%s) values(%s)'
                     % (MSG_COLS, ','.join('?' * 26)), batch)
    print('MSG     : %d 行，%d 个会话   Sequence = CreateTime*1000'
          % (len(batch), stat['talkers']))
    if stat['null_status']:
        print('          其中 %d 行的 Status 源值是 NULL，按客户端常态补了 2'
              % stat['null_status'])

    # 4) MSGTrans
    if src['trans']:
        conn.executemany('insert or replace into MSGTrans(msgLocalId, talkerId) values(?,?)',
                         src['trans'])
        print('MSGTrans: %d 行' % len(src['trans']))
    else:
        print('MSGTrans: 源库没有 TransTable（或为空）—— 跳过')

    # 5) sqlite_sequence
    mx = conn.execute('select max(localId) from MSG').fetchone()[0]
    try:
        conn.execute("update sqlite_sequence set seq=? where name='MSG'", (mx,))
        if conn.execute("select count(*) from sqlite_sequence where name='MSG'"
                        ).fetchone()[0] == 0:
            conn.execute("insert into sqlite_sequence(name,seq) values('MSG',?)", (mx,))
    except sqlite3.Error as e:
        raise SystemExit(
            '写 sqlite_sequence 失败：%s\n'
            '  DDL 里 MSG.localId 必须声明 `INTEGER PRIMARY KEY AUTOINCREMENT`，'
            '否则不会有 sqlite_sequence 表，autoinc 语义也就丢了。' % e)
    print('sequence: MSG = %d' % mx)

    # 6) DBInfo 两行种子
    conn.execute('delete from DBInfo')
    conn.executemany('insert into DBInfo(tableIndex, tableVersion, tableDesc) values(?,?,?)',
                     [(START_TIME_INDEX, start_time, 'Start Time'),
                      (PREFIX_INDEX, PREFIX_VERSION, 'Prefix LocalId Index')])
    print('DBInfo  : Start Time=%d  Prefix LocalId Index=%d' % (start_time, PREFIX_VERSION))

    # 7) 一致性硬校验
    orph = conn.execute(
        'select count(*) from MSG m where not exists'
        ' (select 1 from Name2ID n where n.rowid=m.TalkerId and n.UsrName=m.StrTalker)'
    ).fetchone()[0]
    if orph:
        sample = conn.execute(
            'select localId,TalkerId,StrTalker from MSG m where not exists'
            ' (select 1 from Name2ID n where n.rowid=m.TalkerId and n.UsrName=m.StrTalker)'
            ' limit 10').fetchall()
        conn.close()
        raise SystemExit(
            '有 %d 行的 TalkerId 与 StrTalker 对不上 Name2ID，已中止（产物不可信）：\n  %s\n'
            '  客户端的会话分页按 TalkerId 走索引，对不上会让会话错乱。'
            % (orph, '\n  '.join(map(str, sample))))
    conn.commit()
    conn.close()
    print('校验    : TalkerId / StrTalker / Name2ID 全部自洽')

    # 8) 这是一个**新建**的 3.x 分片，目标格式就是 WAL(2/2) —— 不是改别人的数据。
    #    （空壳起手时已经写成 2/2，这里显式再确认一次并把结果打印出来。）
    mode = wxcom.set_journal_mode(out, wal=True)
    hp = wxcom.truncate_to_header(out)
    hi = wxcom.plain_header_info(out)
    print('收尾    : 日志模式 %s（%s）  按头部页数截断为 %d 页 / %d B'
          % (hi['mode'], hi['hex'], hp, os.path.getsize(out)))
    print('integrity: %s' % wxcom.integrity(out))

    # 9) 加密（给了密钥才做）
    if args.key_hex or args.key_env:
        aes = wxcom.resolve_key(args)
        if not args.salt_from:
            raise SystemExit(
                '给了密钥但没给 --salt-from。加密会**换一把密钥**（客户端按 salt 派生），\n'
                '  必须指明目标客户端库，从它文件头取原本那 16 字节 salt。')
        salt = wxcom.read_salt(args.salt_from)
        n = wxcom.encrypt_db(out, enc_out, aes, salt, journal_mode=(2, 2))
        print('加密    : %s  %d 页  %d B  salt=%s  journal_mode=(2,2)=%s'
              % (enc_out, n, os.path.getsize(enc_out), salt.hex(),
                 wxcom.journal_mode_name(2, 2)))
    else:
        print('提示    : 没给密钥，只产出明文 %s；要给 --key-env/--key-hex 与 --salt-from'
              ' 才会加密成可直接安装的库。' % out)

    print()
    print('Start Time = %d 的作用：客户端只对 Sequence >= 它的消息查本地媒体。'
          % start_time)
    print('  源数据最早的 Sequence 是 %d，所以全部 %d 行都在范围内。' % (min(cts) * 1000, len(spans)))
    print('  副作用：客户端会因此滚出新分片 MSG1.db / MediaMSG1.db / FTSMSG1.db（预期行为）。')
    return 0


# ------------------------------------------------------------------ verify
def cmd_verify(args):
    out = args.out or os.path.join(args.work, 'MSG0.plain.db')
    enc_out = args.enc_out or os.path.join(args.work, 'MSG0.db')
    if not os.path.exists(out):
        raise SystemExit('找不到明文产物：%s（先跑 build）' % out)
    src = read_source(args.src, args.src_table)
    spans = [map_source_row(r, src['idx']) for r in src['rows']]
    cts = [m['CreateTime'] for m in spans]
    start_time = args.start_time if args.start_time is not None else min(cts) * 1000

    fails = []
    c = sqlite3.connect('file:%s?mode=ro' % out, uri=True)

    def check(name, ok, detail=''):
        print('  %-34s %s %s' % (name, 'OK' if ok else 'FAIL', detail))
        if not ok:
            fails.append(name)

    print('=== 明文产物逐项核对 ===')
    print('  integrity: %s' % wxcom.integrity(out))
    check('integrity_check == ok', wxcom.integrity(out) == 'ok')
    n = c.execute('select count(*) from MSG').fetchone()[0]
    check('MSG 行数与源库一致', n == len(spans), '%d vs %d' % (n, len(spans)))
    t = c.execute('select min(CreateTime), max(CreateTime) from MSG').fetchone()
    check('时间跨度与源库一致', list(t) == [min(cts), max(cts)], '%s vs %s' % (list(t), [min(cts), max(cts)]))
    tk = c.execute('select count(distinct TalkerId) from MSG').fetchone()[0]
    check('会话数与源库一致', tk == len({m['TalkerId'] for m in spans}),
          '%d vs %d' % (tk, len({m['TalkerId'] for m in spans})))
    bad_seq = c.execute('select count(*) from MSG where Sequence <> CreateTime*1000').fetchone()[0]
    check('Sequence == CreateTime*1000', bad_seq == 0, '偏离 %d 行' % bad_seq)
    bad_r1 = c.execute('select count(*) from MSG where Reserved1 <> '
                       'case Type when 34 then 2 else 1 end').fetchone()[0]
    check('Reserved1 规则（语音=2）', bad_r1 == 0, '偏离 %d 行' % bad_r1)
    bad_cnt = c.execute(
        'select count(*) from (select TalkerId, count(*) c, min(MsgSequence) lo,'
        ' max(MsgSequence) hi, count(distinct MsgSequence) d from MSG'
        ' group by TalkerId) where lo<>1 or hi<>c or d<>c').fetchone()[0]
    check('MsgSequence 每会话 1..N 连续', bad_cnt == 0, '偏离 %d 个会话' % bad_cnt)
    orph = c.execute('select count(*) from MSG m where not exists'
                     ' (select 1 from Name2ID n where n.rowid=m.TalkerId'
                     '  and n.UsrName=m.StrTalker)').fetchone()[0]
    check('TalkerId 全部可解析且与 StrTalker 一致', orph == 0, '孤儿 %d 行' % orph)
    n2 = c.execute('select count(*) from Name2ID').fetchone()[0]
    check('Name2ID 行数与源库一致', n2 == len(src['name2id']), '%d vs %d' % (n2, len(src['name2id'])))
    di = dict((r[0], r[1]) for r in c.execute('select tableIndex, tableVersion from DBInfo'))
    check('DBInfo.Start Time == 期望值', di.get(START_TIME_INDEX) == start_time,
          '实际 %s 期望 %s' % (di.get(START_TIME_INDEX), start_time))
    check('DBInfo.Prefix LocalId Index 存在', di.get(PREFIX_INDEX) == PREFIX_VERSION)
    sq = c.execute("select seq from sqlite_sequence where name='MSG'").fetchone()
    check('sqlite_sequence == max(localId)',
          bool(sq) and sq[0] == c.execute('select max(localId) from MSG').fetchone()[0])
    head = open(out, 'rb').read(24)
    check('页头保留段 == 48', head[20] == wxcom.RESERVE, 'byte20=%d' % head[20])
    c.close()

    if os.path.exists(enc_out):
        print()
        print('=== 加密产物往返核对 ===')
        if not (args.key_hex or args.key_env):
            print('  （没给密钥，跳过加密产物的核对）')
        else:
            aes = wxcom.resolve_key(args)
            info = wxcom.decrypt_db_ex(enc_out, aes)
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix='.db', prefix='wxrows_')
            os.close(fd)
            try:
                with open(tmp, 'wb') as f:
                    f.write(info['plain'])
                check('HMAC 头部页数之内全通过', info['bad_in_header'] == 0,
                      '头部内 %d / 预分配区 %d / 合计 %d'
                      % (info['bad_in_header'], info['bad_prealloc'], info['bad_total']))
                check('可用区与明文一致', wxcom.usable_diff(out, tmp) == 0)
                check('往返库 integrity == ok', wxcom.integrity(tmp) == 'ok')
                rhi = wxcom.plain_header_info(tmp)
                check('往返库日志模式 == WAL', rhi['mode'] == 'WAL',
                      '%s (%s)' % (rhi['mode'], rhi['hex']))
                c2 = sqlite3.connect('file:%s?mode=ro' % tmp, uri=True)
                try:
                    n2 = c2.execute('select count(*) from MSG').fetchone()[0]
                finally:
                    c2.close()
                check('往返库 MSG 行数一致', n2 == n, '%d vs %d' % (n2, n))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
    else:
        print()
        print('（没有加密产物 %s，跳过往返核对）' % enc_out)

    print()
    print('VERIFY:', 'PASS' if not fails else 'FAIL（%s）' % ', '.join(fails))
    return 0 if not fails else 1


# ------------------------------------------------------------------ install
def cmd_install(args):
    out = args.out or os.path.join(args.work, 'MSG0.plain.db')
    enc_out = args.enc_out or os.path.join(args.work, 'MSG0.db')
    dst = args.install_to
    if not dst:
        raise SystemExit('install 必须给 --install-to <客户端里的那个 MSG0.db>（避免误覆盖源库）')
    if not os.path.exists(enc_out):
        raise SystemExit('找不到加密产物 %s —— 客户端读不了明文，先 build 出加密库' % enc_out)
    with open(enc_out, 'rb') as f:
        if f.read(16) == wxcom.MAGIC:
            raise SystemExit('拒绝安装 %s：它是明文库' % enc_out)
    if cmd_verify(args) != 0:
        raise SystemExit('校验未通过，拒绝安装')
    print()
    wxcom.backup_and_install(enc_out, dst, os.path.join(args.work, 'backups'),
                             force=args.force)
    print('\n完成。请确认客户端已干净退出（-wal 已消失）之后再启动它。')
    return 0


def main():
    return wxcom.run_cli(
        sys.argv[0], __doc__,
        {'check': cmd_check, 'build': cmd_build, 'verify': cmd_verify,
         'install': cmd_install},
        build_args)


if __name__ == '__main__':
    sys.exit(main())
