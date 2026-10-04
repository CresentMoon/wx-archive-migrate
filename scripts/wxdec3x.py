#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxdec3x —— 微信 3.x SQLCipher 库的解密 / 加密 / 回写。

写这一份是因为「已知派生密钥、想把库拿出来看」和「改完想放回去」是两个方向的活，
而且放回去这一步有三个坑必须一起处理对：

  * salt 必须**沿用目标库原本的 16 字节** —— 客户端按它派生密钥，换 salt 等于换密钥；
  * 写回前必须把明文页头的日志模式设回 **WAL(2/2)** —— 这两个字节在 page 0 的密文里，
    事后在密文上改会直接毁掉 HMAC；
  * 必须按**头部页数**截断 —— 客户端的 WCDB 会把库预分配到固定大小
    （见过 52,428,800 / 62,914,560 字节），尾部是裸零的空白页。

动作：

    check    只读：库在不在、多少页、HMAC 坏页、头部页数、预分配余量、integrity
    build    解密到 --out（默认 <work>/<名字>.plain.db）；--encrypt 则反方向加密；
             --to-plain-dir 批量解密一个目录
    verify   重新加密一遍再解回来，比可用区 + 全页 HMAC + integrity（只比可用区，
             因为重新加密必然换新 IV）
    install  把产物写回客户端（--install-to）：先备份、打印还原命令、拒绝明文库

密钥只从 `--key-env` / `--key-hex` 来，脚本里没有任何硬编码。

用法示例：

    # 先看看到底是什么情况（只读，默认动作）
    python3 wxdec3x.py check --db <MSG_DB> --key-env WX_KEY

    # 解密出来
    python3 wxdec3x.py build --db <MSG_DB> --key-env WX_KEY --out ./_wxwork/MSG0.plain.db

    # 校验往返
    python3 wxdec3x.py verify --plain ./_wxwork/MSG0.plain.db --key-env WX_KEY \\
            --salt-from <MSG_DB>

    # 改完之后加密并写回（会先备份，并打印还原命令）
    python3 wxdec3x.py build   --encrypt --db ./_wxwork/MSG0.plain.db \\
            --out ./_wxwork/MSG0.db --salt-from <MSG_DB> --key-env WX_KEY
    python3 wxdec3x.py install --out ./_wxwork/MSG0.db --install-to <MSG_DB> --key-env WX_KEY
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcom                                                     # noqa: E402


# ------------------------------------------------------------------ 参数
def build_args(ap):
    ap.add_argument('--db', metavar='<DB>',
                    help='解密方向：目标 SQLCipher 库；--encrypt 方向：明文库；'
                         '--to-plain-dir 时：目录')
    ap.add_argument('--out', metavar='<FILE>', help='输出文件（默认放在 --work 下）')
    ap.add_argument('--to-plain-dir', metavar='DIR',
                    help='批量解密：把 --db 当目录，其中所有 *.db 解密到这个目录')
    ap.add_argument('--encrypt', action='store_true',
                    help='反方向：把 --db 当明文库加密到 --out（必须给 --salt-from）')
    ap.add_argument('--plain', metavar='<FILE>', help='verify 用的明文（默认 --work 下的那个）')
    ap.add_argument('--install-to', metavar='<DB>', help='install 的目标：客户端里的那个库')
    ap.add_argument('--journal-mode', choices=('keep', 'wal', 'rollback'), default='keep',
                    help='重新加密时写进页头的日志模式；默认 keep = 原样不动（推荐）')
    ap.add_argument('--force', action='store_true',
                    help='install 时忽略「旁边有未检查点的 -wal」这一条告警')
    wxcom.add_common_args(ap)


# ------------------------------------------------------------------ 工具
def plain_path_for(args, db):
    base = os.path.basename(db or 'db')
    return args.out or os.path.join(args.work, base + '.plain.db')


def enc_path_for(args, plain):
    base = os.path.basename(plain).replace('.plain.db', '.db')
    if base == os.path.basename(plain):
        base = os.path.basename(plain) + '.enc.db'
    return os.path.join(args.work, base)


def salt_for(args, need=True):
    if args.salt_from:
        return wxcom.read_salt(args.salt_from)
    if need:
        raise SystemExit('加密必须给 --salt-from <已安装的 3.x 库>：'
                         'salt 要沿用目标库原本的 16 字节，客户端才不会派生出错密钥')
    return None


def is_plaintext(path):
    with open(path, 'rb') as f:
        return f.read(16) == wxcom.MAGIC


def journal_mode_arg(args):
    """把 `--journal-mode` 映射成 `encrypt_db()` 的参数：keep -> None（原样不动）。"""
    m = getattr(args, 'journal_mode', 'keep')
    if m == 'wal':
        return (2, 2)
    if m == 'rollback':
        return (1, 1)
    return None


def report_db(path, aes, compact=False):
    """打印一个 SQLCipher 库的关键指标（只读）。返回 `decrypt_db_ex` 的 info。

    HMAC 必须**分段**看：WCDB 把库预分配到固定尺寸，尾部是裸零空白页，它们的 HMAC
    必然不通过。只有「头部页数之内」的失败才说明密钥不对。
    """
    info = wxcom.decrypt_db_ex(path, aes)
    npages = info['npages']
    hp = info['header_pages']
    if compact:
        verdict = '密钥正确' if info['bad_in_header'] == 0 else '★密钥可能不对'
        print('  %-30s %6d 页  头部 %-6s  头部内坏页 %-4d  预分配区坏页 %-6d  %s'
              % (os.path.basename(path), npages, hp or '?',
                 info['bad_in_header'], info['bad_prealloc'], verdict))
        return info
    size = os.path.getsize(path)
    print('库      : %s' % path)
    print('大小    : %d B  (%.1f 页)' % (size, size / wxcom.PAGE))
    print('页数    : %d' % npages)
    print('头部页数: %s    预分配区: %d 页（WCDB 预分配的裸零空白页，回写时会被截断丢掉）'
          % (hp or '（头部声明不可信）', max(0, npages - hp)))
    print('HMAC    : 头部内 %d   预分配区 %d   合计 %d'
          % (info['bad_in_header'], info['bad_prealloc'], info['bad_total']))
    fd, tmp = tempfile.mkstemp(suffix='.db', prefix='wxdec3x_')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            f.write(info['plain'])
        hi = wxcom.plain_header_info(tmp)
        print('明文页头: %s   page_size=%d 保留段=%d 日志模式=%s'
              % (hi['hex'], hi['page_size'], hi['reserve'], hi['mode']))
        print('          （这是库里真实的值，解密不会改动它）')
        print('integrity: %s' % wxcom.integrity(tmp))
        c = sqlite3.connect('file:%s?mode=ro' % tmp, uri=True)
        try:
            tables = [r[0] for r in c.execute(
                "select name from sqlite_master where type='table' order by name")]
            print('表      : %s' % (', '.join(tables) if tables else '（无）'))
        finally:
            c.close()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return info


def report_plain(path):
    size = os.path.getsize(path)
    hi = wxcom.plain_header_info(path)
    print('明文库  : %s' % path)
    print('大小    : %d B  (%.1f 页)' % (size, size / wxcom.PAGE))
    print('页头    : %s   page_size=%d 保留段=%d 日志模式=%s'
          % (hi['hex'], hi['page_size'], hi['reserve'], hi['mode']))
    print('头部页数: %d' % hi['header_pages'])
    print('integrity: %s' % wxcom.integrity(path))


# ------------------------------------------------------------------ check
def cmd_check(args):
    aes = wxcom.resolve_key(args)
    if args.to_plain_dir:
        src = args.db
        if not src or not os.path.isdir(src):
            raise SystemExit('--to-plain-dir 需要 --db 指向一个目录')
        n = 0
        for f in sorted(os.listdir(src)):
            if not f.endswith('.db'):
                continue                      # 跳过 -wal/-shm；单个库的检查交给单文件模式
            p = os.path.join(src, f)
            try:
                report_db(p, aes, compact=True)
            except SystemExit as e:
                print('  [跳过] %s：%s' % (p, e))
            n += 1
        print('目录里检查了 %d 个 .db（只读，未改动任何东西）' % n)
        return 0
    if not args.db:
        raise SystemExit('必须给 --db <DB>（或 --to-plain-dir + --db <目录>）')
    if args.encrypt:
        report_plain(args.db)
        print('\n--encrypt 方向：build 会加密到 --out；日志模式按 --journal-mode=%s'
              '（默认 keep = 原样不动），并按头部页数截断。' % args.journal_mode)
        return 0
    info = report_db(args.db, aes)
    print()
    if info['bad_in_header']:
        print('结论：**头部页数之内**有 %d 页 HMAC 不通过 —— 这把密钥不是这个库的'
              '（一个库一把派生密钥）。' % info['bad_in_header'])
    else:
        print('结论：密钥正确 —— 头部 %s 页全部通过。'
              % (info['header_pages'] or '?'))
    if info['bad_prealloc']:
        print('      预分配区那 %d 页 HMAC 不通过是**正常**的：WCDB 把库预分配到固定尺寸，'
              '尾部是裸零空白页，不是密钥问题。' % info['bad_prealloc'])
    wal = args.db + '-wal'
    print('旁边有 -wal: %s'
          % (('%d B  <<< 客户端没干净退出，别急着替换主库' % os.path.getsize(wal))
             if os.path.exists(wal) else '否（干净检查点）'))
    return 0


# ------------------------------------------------------------------ build
def cmd_build(args):
    aes = wxcom.resolve_key(args)
    if args.to_plain_dir:
        src, dst = args.db, args.to_plain_dir
        if not src or not os.path.isdir(src):
            raise SystemExit('--to-plain-dir 需要 --db 指向一个目录')
        os.makedirs(dst, exist_ok=True)
        n = ok = 0
        for f in sorted(os.listdir(src)):
            if not f.endswith('.db'):
                continue
            p = os.path.join(src, f)
            n += 1
            try:
                info = wxcom.decrypt_db_ex(p, aes)
            except SystemExit as e:
                print('  [跳过] %s：%s' % (f, e))
                continue
            out = os.path.join(dst, f + '.plain.db')
            with open(out, 'wb') as fh:
                fh.write(info['plain'])
            hp = wxcom.truncate_to_header(out)
            if info['bad_in_header'] == 0:
                ok += 1
            print('  %-26s %6d 页  头部 %-6s  头部内坏页 %-4d  预分配区坏页 %-6d -> %s'
                  % (f, info['npages'], info['header_pages'] or '?',
                     info['bad_in_header'], info['bad_prealloc'], out))
        print('批量解密完成：%d 个库，其中 %d 个「头部页数之内」HMAC 全通过'
              '（已按头部页数截断明文）' % (n, ok))
        return 0

    if not args.db:
        raise SystemExit('必须给 --db')
    if args.encrypt:
        plain = args.db
        if not is_plaintext(plain):
            raise SystemExit('%s 不是 SQLite 明文库（前 16 字节不是 magic）' % plain)
        salt = salt_for(args)
        out = args.out or enc_path_for(args, plain)
        hp = wxcom.truncate_to_header(plain)
        jm = journal_mode_arg(args)
        before = wxcom.plain_header_info(plain)
        print('明文    : %s  (%d 页，头部声明 %d 页)'
              % (plain, os.path.getsize(plain) // wxcom.PAGE, hp))
        if jm is None:
            print('日志模式: 保持 %s（%s）—— 默认不改动' % (before['mode'], before['hex']))
        else:
            print('日志模式: 按要求改成 %d/%d（%s）；原为 %s'
                  % (jm[0], jm[1], wxcom.journal_mode_name(*jm), before['mode']))
            print('          只写进加密产物，不修改 %s 这个文件' % plain)
        print('salt    : %s  <- %s' % (salt.hex(), args.salt_from))
        n = wxcom.encrypt_db(plain, out, aes, salt, journal_mode=jm)
        print('加密    : %s  %d 页  %d B' % (out, n, os.path.getsize(out)))
        print('接下来  : python3 %s verify --plain %s --key-env <VAR> --salt-from %s'
              % (os.path.basename(sys.argv[0]), plain, args.salt_from or '<MSG_DB>'))
        return 0

    info = wxcom.decrypt_db_ex(args.db, aes)
    plain = plain_path_for(args, args.db)
    wxcom.ensure_parent(plain)
    with open(plain, 'wb') as f:
        f.write(info['plain'])
    print('解密    : %s -> %s' % (args.db, plain))
    print('页数    : %d    头部页数: %s    预分配区: %d 页'
          % (info['npages'], info['header_pages'] or '（头部声明不可信）',
             max(0, info['npages'] - info['header_pages'])))
    print('HMAC    : 头部内 %d   预分配区 %d   合计 %d'
          % (info['bad_in_header'], info['bad_prealloc'], info['bad_total']))
    if info['bad_in_header']:
        print('警告    : **头部页数之内**有 %d 页 HMAC 不通过 —— 密钥可能不对，产物不可信。'
              % info['bad_in_header'])
    else:
        print('密钥判定: 正确 —— 头部 %s 页全部通过。'
              % (info['header_pages'] or '?'))
        if info['bad_prealloc']:
            print('          预分配区那 %d 页失败是正常的（裸零空白页），下面会被截断丢掉。'
                  % info['bad_prealloc'])
    hp = wxcom.truncate_to_header(plain)
    print('截断    : 按头部页数 %d 截断 -> %d B（丢掉客户端的预分配空白页）'
          % (hp, os.path.getsize(plain)))
    hi = wxcom.plain_header_info(plain)
    print('页头    : %s  日志模式 %s（原样保留，未改动）' % (hi['hex'], hi['mode']))
    print('integrity: %s' % wxcom.integrity(plain))
    return 1 if info['bad_in_header'] else 0


# ------------------------------------------------------------------ verify
def cmd_verify(args):
    aes = wxcom.resolve_key(args)
    plain = args.plain or plain_path_for(args, args.db)
    if not os.path.exists(plain):
        raise SystemExit('找不到明文：%s（先跑 build，或用 --plain 指定）' % plain)
    if not is_plaintext(plain):
        raise SystemExit('%s 不是 SQLite 明文库' % plain)
    salt = salt_for(args)
    work = args.work
    os.makedirs(work, exist_ok=True)
    rt = os.path.join(work, '_wxdec3x_roundtrip.db')
    tmp_plain = os.path.join(work, '_wxdec3x_reenc_plain.db')
    back_path = os.path.join(work, '_wxdec3x_roundtrip.plain.db')

    # 重新加密 → 再解回来。重新加密必然换新 IV，所以只能比可用区。
    jm = journal_mode_arg(args)
    with open(plain, 'rb') as f:
        data = f.read()
    with open(tmp_plain, 'wb') as f:
        f.write(data)
    n = wxcom.encrypt_db(tmp_plain, rt, aes, salt, journal_mode=jm)
    info = wxcom.decrypt_db_ex(rt, aes)
    with open(back_path, 'wb') as f:
        f.write(info['plain'])
    diff = wxcom.usable_diff(tmp_plain, back_path)
    integ = wxcom.integrity(back_path)
    hi = wxcom.plain_header_info(back_path)

    print('明文            : %s (%d 页)' % (plain, os.path.getsize(plain) // wxcom.PAGE))
    print('重新加密        : %d 页 -> %s' % (n, rt))
    print('再解密          : %d 页   HMAC 头部内 %d / 预分配区 %d / 合计 %d'
          % (info['npages'], info['bad_in_header'], info['bad_prealloc'], info['bad_total']))
    print('可用区不一致页  : %d' % diff)
    print('往返库 integrity: %s' % integ)
    print('往返库日志模式  : %s   （--journal-mode=%s）'
          % (hi['mode'], getattr(args, 'journal_mode', 'keep')))

    ok = (info['bad_in_header'] == 0 and diff == 0 and integ == 'ok')
    for p in (rt, tmp_plain, back_path):
        if os.path.exists(p):
            os.remove(p)
    print('VERIFY:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


# ------------------------------------------------------------------ install
def cmd_install(args):
    aes = wxcom.resolve_key(args)
    src = args.out or enc_path_for(args, args.plain or args.db or '')
    dst = args.install_to
    if not dst:
        raise SystemExit('install 必须给 --install-to <客户端里的那个库>（避免误覆盖）')
    if not src or not os.path.exists(src):
        raise SystemExit('找不到要安装的产物：%s（先跑 build）' % src)
    if is_plaintext(src):
        raise SystemExit(
            '拒绝安装 %s：它是**明文**库。\n'
            '  把明文库放进客户端等于毁掉这个库（客户端读不了）。\n'
            '  请先 --encrypt 加密成 SQLCipher 库再 install。' % src)
    if cmd_verify(args) != 0:
        raise SystemExit('校验未通过，拒绝安装')
    print()
    wxcom.backup_and_install(src, dst, os.path.join(args.work, 'backups'),
                             force=args.force)
    print('\n完成。启动客户端之前请先确认它已经干净退出过（-wal 已消失）。')
    return 0


def main():
    prog = sys.argv[0]
    return wxcom.run_cli(
        prog, __doc__,
        {'check': cmd_check, 'build': cmd_build, 'verify': cmd_verify,
         'install': cmd_install},
        build_args)


if __name__ == '__main__':
    sys.exit(main())
