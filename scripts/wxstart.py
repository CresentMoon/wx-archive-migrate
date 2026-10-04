#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxstart —— 读写 3.x 库里的 `DBInfo.'Start Time'`。**语音能不能播就看这一个值。**

## 机制（实测确认）

`Msg/Multi/MSG0.db` 里有一张两行的 `DBInfo` 表（客户端自己写的），其中

    tableIndex=1  tableDesc='Start Time'           tableVersion = 毫秒时间戳
    tableIndex=2  tableDesc='Prefix LocalId Index'  tableVersion = 索引版本

`Start Time` 是**本分片纪元**，客户端用它判断一条消息是不是「本库里长出来的」：

    Sequence >= StartTime  ->  先查本地媒体（MediaMSG0.Media），命中即可播放
    Sequence <  StartTime  ->  当成从别处导入的历史，**跳过本地媒体**，直接去服务器下载

从别处导入的历史记录，其媒体通常没被一起带过来；而历史消息的 CDN 早已失效，
于是客户端表现为：气泡上显示着时长，点下去是红色感叹号或无限转圈，再点问「是否重新下载」。
把 `Start Time` 挪到**早于全部数据**之后，这些消息就重新落进「本库」范围，本地媒体被查到，
语音立刻能播。

## 怎么判、怎么修

    check    只读：打印 DBInfo 全部行、min/max(Sequence)，并直接判定能不能播
    build    把 Start Time 改成 --set <毫秒> 或 --auto（= min(Sequence)）
    verify   重新读出产物，确认 DBInfo 正确、integrity ok、MSG 行数没变
    install  写回客户端（--install-to）：先备份、打印还原命令

## 副作用（预期行为，不必惊慌）

把 `Start Time` 前移之后，客户端会认为当前分片「太老」，于是**滚出一个新分片**：

    Msg/Multi/MSG1.db  +  MediaMSG1.db  +  FTSMSG1.db

之后收到的新消息落在 MSG1 里，历史数据仍在 MSG0 里 —— 客户端会同时读多个分片。
（也就是说这个字段同时承担了「分片纪元」的语义，两个语义是同一个值。）

用法示例：

    export WX_KEY=<AES_KEY>
    python3 wxstart.py check   --db <MSG_DB> --key-env WX_KEY
    python3 wxstart.py build   --db <MSG_DB> --key-env WX_KEY --auto \\
            --out ./_wxwork/MSG0.plain.db --enc-out ./_wxwork/MSG0.new.db
    python3 wxstart.py verify  --db <MSG_DB> --key-env WX_KEY --auto
    python3 wxstart.py install --db <MSG_DB> --key-env WX_KEY --auto --install-to <MSG_DB>
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcom                                                     # noqa: E402

START_INDEX = 1
PREFIX_INDEX = 2


# ------------------------------------------------------------------ 参数
def build_args(ap):
    ap.add_argument('--db', metavar='<DB>', help='目标 3.x 库（客户端里的那个，必需）')
    ap.add_argument('--set', metavar='<MS>', type=int,
                    help='把 Start Time 设成这个毫秒时间戳')
    ap.add_argument('--auto', action='store_true',
                    help='自动取 min(Sequence)（推荐：恰好等于最早一条消息的时间）')
    ap.add_argument('--out', metavar='<FILE>', help='明文输出（默认 <work>/<名字>.plain.db）')
    ap.add_argument('--enc-out', metavar='<FILE>', help='加密输出（默认 <work>/<名字>.new.db）')
    ap.add_argument('--install-to', metavar='<DB>', help='install 目标（显式给出，避免误覆盖）')
    ap.add_argument('--journal-mode', choices=('keep', 'wal', 'rollback'), default='keep',
                    help='重新加密时写进页头的日志模式；默认 keep = 沿用库里原本的值（推荐）')
    ap.add_argument('--force', action='store_true',
                    help='install 时忽略「旁边有未检查点的 -wal」这一条告警')
    wxcom.add_common_args(ap)


# ------------------------------------------------------------------ 工具
def paths(args):
    base = os.path.basename(args.db or 'db')
    plain = args.out or os.path.join(args.work, base + '.plain.db')
    enc = args.enc_out or os.path.join(args.work, base + '.new.db')
    return plain, enc


def target_value(args, conn):
    """算出要写的值：--set 优先，其次 --auto。都没有则返回 None。"""
    if args.set is not None:
        return args.set, '来自 --set'
    if args.auto:
        mn = conn.execute('select min(Sequence) from MSG').fetchone()[0]
        if mn is None:
            raise SystemExit('MSG 是空的，min(Sequence) 取不到')
        return mn, '来自 --auto = min(Sequence)'
    return None, None


def journal_mode_arg(args):
    """`--journal-mode` -> `encrypt_db()` 的参数；keep 返回 None（原样不动）。"""
    m = getattr(args, 'journal_mode', 'keep')
    if m == 'wal':
        return (2, 2)
    if m == 'rollback':
        return (1, 1)
    return None


def load_db(args, aes, out):
    """解密到 out，返回 (info, 明文路径)。info 见 `wxcom.decrypt_db_ex`。

    报告时一律用「头部页数之内」的 HMAC 失败数判断密钥对错：WCDB 预分配的尾部空白页
    必然 HMAC 不通过，那是正常的。
    """
    info = wxcom.decrypt_db_ex(args.db, aes)
    with open(out, 'wb') as f:
        f.write(info['plain'])
    return info, out


def print_hmac(info):
    print('页数    : %d    头部页数: %s    预分配区: %d 页'
          % (info['npages'], info['header_pages'] or '（头部声明不可信）',
             max(0, info['npages'] - info['header_pages'])))
    print('HMAC    : 头部内 %d   预分配区 %d   合计 %d'
          % (info['bad_in_header'], info['bad_prealloc'], info['bad_total']))
    if info['bad_in_header']:
        print('          ★ 头部页数之内有失败 —— 密钥可能不对。')
    elif info['bad_prealloc']:
        print('          预分配区失败是正常的（WCDB 预留的裸零空白页），不影响判断。')


def verdict(conn, start_time):
    """打印判定，返回 `(能不能播, 早于 StartTime 的行数)`。"""
    mn, mx, n = conn.execute('select min(Sequence), max(Sequence), count(*) from MSG'
                             ).fetchone()
    print('  消息 Sequence 范围 : %s .. %s  （%d 行）' % (mn, mx, n))
    print('  当前 Start Time     : %s' % start_time)
    if mn is None:
        print('  判定               : 库里没有消息，无从判定')
        return False, 0
    if start_time is None:
        print('  判定               : 没有 Start Time 这一行 —— 客户端行为未定义，'
              '建议按 --auto 写入')
        return False, n
    try:
        st = int(start_time)
    except (TypeError, ValueError):
        print('  判定               : Start Time 不是整数（%r），无法判定' % (start_time,))
        return False, n
    late = conn.execute('select count(*) from MSG where Sequence < ?', (st,)).fetchone()[0]
    if late == 0:
        print('  判定               : ✅ 可以播放 —— Start Time 不晚于最早一条消息，'
              '全部 %d 行都会先查本地媒体' % n)
        return True, 0
    print('  判定               : ❌ 播不了 —— 有 %d 行早于 Start Time，'
          '它们会被当成导入的历史而跳过本地媒体' % late)
    print('  建议值             : %d  （= min(Sequence)，即 --auto）' % mn)
    return False, late


# ------------------------------------------------------------------ check
def cmd_check(args):
    if not args.db:
        raise SystemExit('必须给 --db <3.x 库>')
    aes = wxcom.resolve_key(args)
    fd, tmp = tempfile.mkstemp(suffix='.db', prefix='wxstart_')
    os.close(fd)
    try:
        info, plain = load_db(args, aes, tmp)
        print('库      : %s' % args.db)
        print_hmac(info)
        print('integrity: %s' % wxcom.integrity(plain))
        hi = wxcom.plain_header_info(plain)
        print('页头    : %s  日志模式 %s（解密不改动它）'
              % (hi['hex'], hi['mode']))
        c = sqlite3.connect('file:%s?mode=ro' % plain, uri=True)
        print()
        print('=== DBInfo 全部行 ===')
        rows = wxcom.db_info_all(c)
        if not rows:
            print('  （空）')
        for r in rows:
            extra = ''
            if r[0] == START_INDEX:
                extra = "   <- 'Start Time'（本分片纪元）"
            elif r[0] == PREFIX_INDEX:
                extra = "   <- 'Prefix LocalId Index'"
            print('  (tableIndex=%s, tableVersion=%s, tableDesc=%r)%s' % (r[0], r[1], r[2], extra))

        mn, mx, n = c.execute('select min(Sequence), max(Sequence), count(*) from MSG').fetchone()
        start = wxcom.db_info_get(c, START_INDEX)
        print()
        print('=== 判定 ===')
        ok, late = verdict(c, start[1] if start else None)
        if late:
            print('             受影响的 %d 行里，语音（Type=34）占 %d 行'
                  % (late, c.execute('select count(*) from MSG where Type=34'
                                     ' and Sequence < ?',
                                     (int(start[1]),)).fetchone()[0]))
        c.close()
        print()
        print('修复：python3 %s build --db %s --key-env <VAR> --auto'
              % (os.path.basename(sys.argv[0]), args.db))
        print('副作用：客户端会滚出新分片 MSG1.db / MediaMSG1.db / FTSMSG1.db（预期行为）。')
        return 0 if ok else 0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ------------------------------------------------------------------ build
def cmd_build(args):
    if not args.db:
        raise SystemExit('必须给 --db <3.x 库>')
    aes = wxcom.resolve_key(args)
    plain, enc = paths(args)
    fd, tmp = tempfile.mkstemp(suffix='.db', prefix='wxstart_')
    os.close(fd)
    try:
        info, tmp = load_db(args, aes, tmp)
        print_hmac(info)
        c = sqlite3.connect(tmp)
        value, why = target_value(args, c)
        if value is None:
            raise SystemExit('必须给 --set <毫秒> 或 --auto（--auto 取 min(Sequence)，推荐）')
        old = wxcom.db_info_get(c, START_INDEX)
        mn, mx, n = c.execute('select min(Sequence), max(Sequence), count(*) from MSG').fetchone()
        wxcom.db_info_set(c, value, START_INDEX, 'Start Time')
        wxcom.db_info_set(c, 1, PREFIX_INDEX, 'Prefix LocalId Index')
        print('DBInfo  : Start Time %s -> %d   (%s)'
              % (old[1] if old else '（原本没有）', value, why))
        print('          Prefix LocalId Index = 1')
        print('消息    : %d 行，Sequence %s .. %s' % (n, mn, mx))
        if value > mn:
            print('警告    : 新值大于 min(Sequence)=%d —— 早于它的消息仍然播不了。' % mn)
        c.close()

        # 明文产物（供 verify / 人工检查）
        import shutil
        wxcom.ensure_parent(plain)
        shutil.copy2(tmp, plain)
        jm = journal_mode_arg(args)
        if jm is not None:
            wxcom.set_journal_mode(plain, wal=(jm == (2, 2)))
            print('日志模式: 按要求改成 %s（原为 %s）'
                  % (wxcom.journal_mode_name(*jm),
                     wxcom.plain_header_info(tmp)['mode']))
        hp = wxcom.truncate_to_header(plain)
        hi = wxcom.plain_header_info(plain)
        print('明文    : %s  %d 页  日志模式 %s（%s）'
              % (plain, hp, hi['mode'], hi['hex']))
        print('integrity: %s' % wxcom.integrity(plain))

        salt = wxcom.read_salt(args.salt_from or args.db)
        k = wxcom.encrypt_db(plain, enc, aes, salt, journal_mode=jm)
        print('加密    : %s  %d 页  %d B  salt=%s' % (enc, k, os.path.getsize(enc), salt.hex()))
        print()
        print('接下来  : python3 %s install --db %s --key-env <VAR> --auto --install-to %s'
              % (os.path.basename(sys.argv[0]), args.db, args.db))
        return 0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ------------------------------------------------------------------ verify
def cmd_verify(args):
    if not args.db:
        raise SystemExit('必须给 --db <3.x 库>')
    aes = wxcom.resolve_key(args)
    plain, enc = paths(args)
    if not os.path.exists(plain):
        raise SystemExit('找不到明文产物 %s（先跑 build）' % plain)
    fails = []
    c = sqlite3.connect('file:%s?mode=ro' % plain, uri=True)
    mn, mx, n = c.execute('select min(Sequence), max(Sequence), count(*) from MSG').fetchone()
    start = wxcom.db_info_get(c, START_INDEX)
    c.close()

    def check(name, ok, detail=''):
        print('  %-36s %s %s' % (name, 'OK' if ok else 'FAIL', detail))
        if not ok:
            fails.append(name)

    print('=== 明文产物 ===')
    check('integrity_check == ok', wxcom.integrity(plain) == 'ok')
    hi = wxcom.plain_header_info(plain)
    check('页头保留段 == 48', hi['reserve'] == wxcom.RESERVE,
          'byte20=%d' % hi['reserve'])
    check('日志模式与预期一致',
          journal_mode_arg(args) is None or (hi['write_ver'], hi['read_ver']) == journal_mode_arg(args),
          '%s (--journal-mode=%s)' % (hi['mode'], getattr(args, 'journal_mode', 'keep')))
    check('DBInfo.Start Time 存在', start is not None)
    if start is not None:
        check('Start Time <= min(Sequence)', int(start[1]) <= mn,
              '%s <= %s' % (start[1], mn))
        check('Start Time == 期望值（--set/--auto）',
              (args.set is None and not args.auto) or int(start[1]) == (
                  args.set if args.set is not None else mn),
              '实际 %s' % start[1])
    c2 = sqlite3.connect('file:%s?mode=ro' % plain, uri=True)
    try:
        check('Prefix LocalId Index == 1',
              wxcom.db_info_get(c2, PREFIX_INDEX)[1] == 1)
    finally:
        c2.close()
    check('MSG 行数 > 0（没有被清空）', n > 0, '%d 行' % n)

    if os.path.exists(enc):
        print()
        print('=== 加密产物往返 ===')
        info = wxcom.decrypt_db_ex(enc, aes)
        fd, tmp = tempfile.mkstemp(suffix='.db', prefix='wxstart_rt_')
        os.close(fd)
        try:
            with open(tmp, 'wb') as f:
                f.write(info['plain'])
            check('HMAC 头部页数之内全通过', info['bad_in_header'] == 0,
                  '头部内 %d / 预分配区 %d / 合计 %d'
                  % (info['bad_in_header'], info['bad_prealloc'], info['bad_total']))
            check('可用区与明文一致', wxcom.usable_diff(plain, tmp) == 0)
            check('往返库 integrity == ok', wxcom.integrity(tmp) == 'ok')
            c3 = sqlite3.connect('file:%s?mode=ro' % tmp, uri=True)
            try:
                n3 = c3.execute('select count(*) from MSG').fetchone()[0]
                s3 = wxcom.db_info_get(c3, START_INDEX)
            finally:
                c3.close()
            check('往返库 MSG 行数一致', n3 == n, '%d vs %d' % (n3, n))
            check('往返库 Start Time 一致',
                  s3 is not None and (start is None or s3[1] == start[1]))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    else:
        print()
        print('（没有加密产物 %s，跳过往返核对）' % enc)

    print()
    print('VERIFY:', 'PASS' if not fails else 'FAIL（%s）' % ', '.join(fails))
    return 0 if not fails else 1


# ------------------------------------------------------------------ install
def cmd_install(args):
    if not args.db:
        raise SystemExit('必须给 --db <3.x 库>')
    aes = wxcom.resolve_key(args)
    plain, enc = paths(args)
    dst = args.install_to
    if not dst:
        raise SystemExit('install 必须显式给 --install-to <库路径>（避免误覆盖别的库）')
    if not os.path.exists(enc):
        raise SystemExit('找不到加密产物 %s（先跑 build）' % enc)
    with open(enc, 'rb') as f:
        if f.read(16) == wxcom.MAGIC:
            raise SystemExit('拒绝安装 %s：它是明文库' % enc)
    if cmd_verify(args) != 0:
        raise SystemExit('校验未通过，拒绝安装')
    print()
    wxcom.backup_and_install(enc, dst, os.path.join(args.work, 'backups'),
                             force=args.force)
    print('\n完成。启动客户端后：')
    print('  * 历史语音应当可以直接播放；')
    print('  * 客户端会新建 MSG1.db / MediaMSG1.db / FTSMSG1.db（这是预期的分片滚动）。')
    return 0


def main():
    return wxcom.run_cli(
        sys.argv[0], __doc__,
        {'check': cmd_check, 'build': cmd_build, 'verify': cmd_verify,
         'install': cmd_install},
        build_args)


if __name__ == '__main__':
    sys.exit(main())
