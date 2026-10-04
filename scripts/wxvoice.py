#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxvoice.py — 把 2.x 的语音（Type=34）写进 3.x 的媒体库，并对齐消息行的 XML。

语音是最特殊的一类：它**不在文件系统里**
----------------------------------------
图片/视频/文件都在 `FileStorage\\` 下，语音**在数据库里**。3.9 的语音媒体表是：

    Msg/Multi/MediaMSG0.db   表 Media(Key TEXT PRIMARY KEY, Reserved0 INT,
                                      Buf BLOB, Reserved1 INT, Reserved2 TEXT)

    权威样例（客户端自己发出的那条语音，实测）：
        Key        = 2**40 + MSG.localId      （十进制字符串）
        Reserved0  = MSG.MsgSvrID
        Buf        = 音频字节，\x02#!SILK_V3（SILK）或 #!AMR（AMR）
        Reserved1 / Reserved2 = NULL

**不要把旧版的 `Msg/Media.db.MediaInfo` 整表搬过来。** 那张表属于「聊天记录 / 收藏」
子系统：客户端自己**建了表却从不写**。我们当初搬了 107 行进去，实测**有没有它都能播**，
而且其中 7 行还指向根本不存在的 localId。（这条已经独立验证过，所以本工具不再碰它。）

为什么「播放失败」跟 XML 里的字段无关 —— 以及真正的闸门
-------------------------------------------------------
实测的失败长相：气泡显示时长，点了打红色感叹号／无限转圈，再点问「是否重新下载」。

做过一组**单变量对照**（把能播的那条复制成多份，每份只改一个字段），结论是：
音频字节、容器格式、`msgsource`、`MsgSvrID`、`MsgSequence`、会话、**乃至有没有本地媒体行**
—— 全都不影响。唯一有关系的是**消息自己的时间**：

    Msg/Multi/MSG0.db 的 DBInfo(tableIndex=1, tableDesc='Start Time')
    是「本分片纪元」：
        Sequence >= StartTime  ->  先查本地媒体，命中即播
        Sequence <  StartTime  ->  当成从别处导入的历史，**跳过本地媒体**直接去服务器下载

2.x 导入进来的历史消息，`Sequence` 全是十年前的毫秒值，天然都小于「本分片纪元」，
于是条条被绕开；当年的 CDN 早已失效，所以永远播放失败。
把 `Start Time` 改到 ≤ 最早一条消息的 `Sequence` 之后，**105/105 条语音全部可播**，
而且客户端跨重启**不会把它改回去**（实测确认）。

所以：`check` 会**大声警告**这一点；真正改它的是同目录的 `wxstart.py`。

CLI 契约
--------
    wxvoice.py check | build | verify | install [选项]

    check    只读：语音行数 / Media 行数 / 能对上几条 / 音频长度与 XML 是否一致 /
             **Start Time 是否 ≤ min(Sequence)**
    build    写 Media 行 + 对齐 MSG 行的 XML（不碰客户端）
    verify   全页 HMAC + 可用区往返 + integrity + 逐行核对 Key/Reserved0/Buf/魔数
    install  **先备份再替换** MSG*.db 与 MediaMSG*.db，打印备份路径与还原命令

示例
----
    python3 wxvoice.py check --wechat-dir "<DATA_ROOT>" --account <ACCOUNT> \\
            --archive-media <PLAIN_2X_MEDIA_DB> --key-env WX_MSG_KEY \\
            --media-key-env WX_MEDIA_KEY
    python3 wxvoice.py build ... && python3 wxvoice.py verify ... \\
            && python3 wxvoice.py install ...

安装前请让客户端**干净退出**（托盘右键 → 退出微信），或用 `install_guard.sh` 包住本命令。
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time

# ------------------------------------------------------------------ wxcom
# 延迟导入：wxcom 尚未就位时也要能正常打印 --help（退出码 0）。
try:
    import wxcom as _wxcom
except ImportError:                                    # pragma: no cover
    _wxcom = None

KEY_BIAS = 1 << 40          # 前缀 localId 的基数：Key = 2**40 + localId
SILK_MAGIC = b'\x02#!SILK_V3'
AMR_MAGIC = b'#!AMR'

USAGE = """用法：
    wxvoice.py check | build | verify | install [选项]

必需参数：
    --wechat-dir <DATA_ROOT>      微信数据根（其下有 <账号> 目录）
    --account    <ACCOUNT>        账号目录名
    --key-hex <AES_KEY> 或 --key-env <VAR>          MSG*.db 的密钥
    --media-key-hex <KEY> 或 --media-key-env <VAR>  MediaMSG*.db 的密钥（**每库一把，不相同**）
    --archive-media <PLAIN_2X_MEDIA_DB>  build 用：解密后的 2.x Media.db（音频来源）

常见可选参数：
    --msg-db <MSG_DB>       默认 <账号>/Msg/Multi/MSG0.db
    --media-db <MEDIA_DB>   默认 <账号>/Msg/Multi/MediaMSG0.db
    --work <DIR>            工作目录（默认 ./_wxwork）
    --backup-dir <DIR>      install 的备份目录
    --fix-length            顺手把 XML 的 length= 改成与音频字节一致（默认不改）"""


def _need(*names):
    if _wxcom is None:
        raise SystemExit('缺少共享库：与本脚本同目录的 wxcom/（见 scripts/wxcom/__init__.py）')
    missing = [n for n in names if getattr(_wxcom, n, None) is None]
    if missing:
        raise SystemExit('wxcom 缺少这些接口：%s' % ', '.join(missing))
    return _wxcom


def _key_of(args, hex_attr, env_attr, what):
    envv = getattr(args, env_attr, '')
    hexv = getattr(args, hex_attr, '')
    if envv:
        v = os.environ.get(envv)
        if not v:
            raise SystemExit('环境变量 %s 是空的' % envv)
        return bytes.fromhex(v.strip())
    if hexv:
        return bytes.fromhex(hexv.strip())
    raise SystemExit('必须给 --%s 或 --%s（%s）'
                     % (hex_attr.replace('_', '-'), env_attr.replace('_', '-'), what))


def msg_key(args):
    return _key_of(args, 'key_hex', 'key_env', 'MSG*.db 的 AES 密钥')


def media_key(args):
    return _key_of(args, 'media_key_hex', 'media_key_env', 'MediaMSG*.db 的 AES 密钥')


# ------------------------------------------------------------------ 小工具
def codec_of(buf):
    if buf[:len(SILK_MAGIC)] == SILK_MAGIC:
        return 'SILK'
    if buf[:len(AMR_MAGIC)] == AMR_MAGIC:
        return 'AMR'
    return 'UNKNOWN'


def expected_length(buf):
    """XML 里 length= 的约定（实测）：SILK 就是字节数；AMR 要减掉 6 字节 magic。

    拿不准的时候宁可**不改** —— 客户端只把它当元数据，改错了反而更糟。
    """
    c = codec_of(buf)
    if c == 'SILK':
        return len(buf)
    if c == 'AMR':
        return max(0, len(buf) - len(AMR_MAGIC) - 1)
    return None


def xml_length(sc):
    m = re.search(r'(?<![a-zA-Z])length="(\d+)"', sc or '')
    return int(m.group(1)) if m else None


def db_info_get(c, idx):
    """读 `DBInfo(tableIndex=idx)` 的 **tableVersion**（标量）。

    共享库的 `db_info_get()` 返回的是整行 `(tableIndex, tableVersion, tableDesc)`，
    这里只取 tableVersion（第 2 个字段）—— 当初按"返回标量"用，读出来是个元组，
    比较大小直接报错，是个真 bug。
    """
    row = _need('db_info_get').db_info_get(c, idx)
    return row[1] if row else None


def ts(v):
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(v / 1000.0))
    except Exception:
        return str(v)


def archive_audio(path):
    """解密后的 2.x Media.db -> {MsgLocalId: 音频字节}

    2.x 把语音音频放在 MediaInfo 的 Thumbnail 列（Detail 列多数为 NULL），
    这是当年客户端自己的存法；`MsgLocalId` 与 3.x 的 `MSG.localId` 直接对应。
    """
    if not path or not os.path.exists(path):
        raise SystemExit('--archive-media 指向的解密库不存在：%r' % path)
    c = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
    cols = {r[1] for r in c.execute('PRAGMA table_info(MediaInfo)')}
    if 'MsgLocalId' not in cols:
        raise SystemExit('%s 里没有 MediaInfo(MsgLocalId) 表 —— 是不是解密的 2.x Media.db？' % path)
    pick = 'Thumbnail' if 'Thumbnail' in cols else 'Detail'
    out = {}
    for lid, buf in c.execute('select MsgLocalId,%s from MediaInfo' % pick):
        if buf:
            out[lid] = buf
    c.close()
    return out


# ------------------------------------------------------------------ 路径 / 解密
def paths_of(args):
    """-> `(acct_root, msg_db, media_db)`。

    **注意顺序**：本函数第 1 个就是账号目录（与兄弟脚本 `wxmedia.py` 的 `paths_of()`
    不同 —— 那个先返回数据根）。改调用点前先看清这里返回什么。
    """
    root = os.path.abspath(os.path.expanduser(args.wechat_dir))
    acct_root = os.path.join(root, args.account)
    if not os.path.isdir(acct_root):
        raise SystemExit('账号目录不存在：%s（检查 --wechat-dir / --account）' % acct_root)
    msg_db = args.msg_db or os.path.join(acct_root, 'Msg', 'Multi', 'MSG0.db')
    media_db = args.media_db or os.path.join(acct_root, 'Msg', 'Multi', 'MediaMSG0.db')
    return acct_root, msg_db, media_db


def key_verdict(plain, bad, npages):
    """判断"密钥对不对" —— **不要**看页 HMAC 失败总数。

    坑：客户端会把库**预分配**到固定大小（见过 52 MB / 62 MB），尾部空白页是裸零、
    不是合法 SQLCipher 页，解密时必然 HMAC 失败。所以「失败页数 > 0」**不等于**密钥错，
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


def decrypt_to(src, key, dst_path, label):
    _need('decrypt_db', 'header_pages')
    b, bad, npages = _wxcom.decrypt_db(src, key)
    with open(dst_path, 'wb') as f:
        f.write(b)
    hp, slack, verdict, magic_ok = key_verdict(dst_path, bad, npages)
    print('解密 %-12s %s -> %s' % (label, src, dst_path))
    print('  头部页数 %d / 文件页数 %d（预分配空白 %d 页）；页 HMAC 未通过 %d 页'
          % (hp, npages, slack, bad))
    print('  密钥判定：%s' % verdict)
    if not magic_ok:
        raise SystemExit('密钥不对，先别往下走（明文不是合法 SQLite 库）。')
    return bad, npages


def voice_rows(c):
    """[(localId, talker, MsgSvrID, CreateTime, Sequence)]"""
    return list(c.execute('select localId,StrTalker,MsgSvrID,CreateTime,Sequence '
                          'from MSG where Type=34 order by localId'))


def media_rows(m):
    return {r[0]: r for r in m.execute('select Key,Reserved0,length(Buf),Buf from Media')}


# ------------------------------------------------------------------ check
def cmd_check(args):
    _need('integrity', 'decrypt_db', 'read_salt')
    acct_root, msg_db, media_db = paths_of(args)
    os.makedirs(args.work, exist_ok=True)
    plain_msg = os.path.join(args.work, '_plain_voice_msg.db')
    plain_media = os.path.join(args.work, '_plain_voice_media.db')
    bad1, _ = decrypt_to(msg_db, msg_key(args), plain_msg, 'MSG*.db')
    bad2, _ = decrypt_to(media_db, media_key(args), plain_media, 'MediaMSG*.db')
    c = sqlite3.connect(plain_msg)
    m = sqlite3.connect(plain_media)
    print('integrity: MSG=%s  Media=%s' % (_wxcom.integrity(plain_msg), _wxcom.integrity(plain_media)))

    rows = voice_rows(c)
    audio = archive_audio(args.archive_media) if args.archive_media else {}
    have = [r for r in rows if r[0] in audio]
    md = media_rows(m)

    print('\n=== 语音 (Type=34) ===')
    print('  消息行                 : %d 条' % len(rows))
    print('  归档里有音频的         : %d 条' % len(have))
    if args.archive_media:
        print('  归档 MediaInfo 音频条数: %d' % len(audio))
    print('  Media 表现有行数       : %d' % len(md))

    match = magic = lenbad = 0
    for lid, _t, svr, _ct, _sq in have:
        buf = audio[lid]
        r = md.get(str(KEY_BIAS + lid))
        if r and r[1] == svr and r[2] == len(buf):
            match += 1
        if codec_of(buf) == 'UNKNOWN':
            magic += 1
        sc = c.execute('select StrContent from MSG where localId=?', (lid,)).fetchone()[0]
        xl = xml_length(sc)
        exp = expected_length(buf)
        if xl is not None and exp is not None and xl != exp:
            lenbad += 1
    print('  Media 行能对上（Key/Reserved0/长度）: %d / %d' % (match, len(have)))
    if have:
        kinds = {}
        for lid, _t, _s, _c, _q in have:
            k = codec_of(audio[lid])
            kinds[k] = kinds.get(k, 0) + 1
        print('  音频格式分布           : %s' % kinds)
    if magic:
        print('  ！魔数不是 SILK/AMR 的音频: %d 条（这两者之外客户端解不了）' % magic)
    print('  XML length= 与音频不一致  : %d 条%s'
          % (lenbad, '（可用 --fix-length 修）' if lenbad else ''))

    # ---- 真正的闸门：DBInfo 的「本分片纪元」
    start = db_info_get(c, 1)
    if start is None:
        print('\n！！ 读不到 DBInfo(tableIndex=1) 的 Start Time —— 这个库可能不是客户端建的。')
    else:
        minseq = c.execute('select min(Sequence) from MSG').fetchone()[0]
        minv = c.execute('select min(Sequence) from MSG where Type=34').fetchone()[0]
        print('\n=== DBInfo「本分片纪元」(Start Time) ===')
        print('  Start Time            : %d  (%s)' % (start, ts(start)))
        print('  全库 min(Sequence)    : %s  (%s)' % (minseq, ts(minseq)))
        print('  语音行 min(Sequence)  : %s  (%s)' % (minv, ts(minv)))
        if minseq is not None and start > minseq:
            print('\n' + '！' * 30)
            print('！！ 这些语音在客户端里 **播不了**。')
            print('！！ 原因：Start Time 比最早一条消息的 Sequence 还晚 —— 客户端会把')
            print('！！   Sequence < StartTime 的消息当成「从别处导入的历史」，**跳过本地媒体**')
            print('！！   直接去服务器下载；当年的 CDN 早已失效，于是红色感叹号／无限转圈。')
            print('！！ 修法：用 wxstart.py 把 Start Time 设到 ≤ %s（或 ≤ 最早一条的 Sequence）。' % minseq)
            print('！' * 30)
        else:
            print('  ⇒ Start Time ≤ min(Sequence)，语音可以走本地媒体（实测这就是能播的条件）。')

    if bad1 or bad2:
        print('\n（页 HMAC 的失败数别当判据：预分配空白页天然不通过。上面的「密钥判定」才是。）')
    c.close()
    m.close()
    return 0


# ------------------------------------------------------------------ build
def patch_xml(sc, lid, audio, fix_length):
    """把一条语音的 XML 对齐成 3.x 客户端自己写出来的形状。

    这些补丁值是**按 localId 确定性生成**的（md5），所以反复跑是幂等的。
    aeskey / voiceurl 只是占位：它们只用于 CDN 下载，而我们本来就是本地音频；
    就算客户端真去下，结果和现在一样失败，不会更糟。
    """
    new = sc or ''
    new = re.sub(r'bufid="[^"]*"', 'bufid="0"', new)
    if 'aeskey=' not in new:
        aes = hashlib.md5(('aes%d' % lid).encode()).hexdigest()
        # 真实样例的 voiceurl 是 232 位十六进制、以 7f0c0002 开头；这里保持同样的长度与字符集
        url = '7f0c0002' + (hashlib.md5(('url%d' % lid).encode()).hexdigest() * 7)[:224]
        new = new.replace('bufid="0"',
                          'bufid="0" aeskey="%s" voiceurl="%s" voicemd5=""' % (aes, url), 1)
    if 'silklength=' not in new and 'voiceformat="4"' in new:
        # 不能判断 new.rstrip().endswith('/>') —— 我们的 XML 结尾是 </msg>，
        # 早先这么写导致 silklength 一条都没加上（靠 verify 才抓出来）。
        new = re.sub(r'\s*/>', ' silklength="0" />', new, count=1)
    if fix_length and audio:
        exp = expected_length(audio)
        if exp is not None:
            # 坑：(?<![a-zA-Z]) 不能省 —— 裸的 length="..." 会匹配进 voicelength="..." 里，
            # 把时长改成字节数，界面上就会显示成「15311 秒」。
            new = re.sub(r'(?<![a-zA-Z])length="[^"]*"', 'length="%d"' % exp, new, count=1)
    return new


def cmd_build(args):
    _need('integrity', 'encrypt_db', 'read_salt', 'truncate_to_header', 'decrypt_db',
          'set_journal_mode', 'header_pages')
    acct_root, msg_db, media_db = paths_of(args)
    os.makedirs(args.work, exist_ok=True)
    plain_msg = os.path.join(args.work, '_plain_voice_msg.db')
    plain_media = os.path.join(args.work, '_plain_voice_media.db')
    decrypt_to(msg_db, msg_key(args), plain_msg, 'MSG*.db')
    decrypt_to(media_db, media_key(args), plain_media, 'MediaMSG*.db')

    audio = archive_audio(args.archive_media)
    c = sqlite3.connect(plain_msg)
    m = sqlite3.connect(plain_media)
    rows = voice_rows(c)
    print('MSG 里 Type=34 共 %d 条；归档里有音频的 %d 条'
          % (len(rows), sum(1 for r in rows if r[0] in audio)))

    # (1) Media 行：Key = 2**40 + localId, Reserved0 = MsgSvrID, Buf = 音频
    ins = skip = nomedia = 0
    for lid, _t, svr, _ct, _sq in rows:
        buf = audio.get(lid)
        if not buf:
            nomedia += 1
            continue
        key = str(KEY_BIAS + lid)
        if m.execute('select count(*) from Media where Key=?', (key,)).fetchone()[0]:
            skip += 1
            continue
        m.execute('insert into Media(Key,Reserved0,Buf,Reserved1,Reserved2) '
                  'values(?,?,?,NULL,NULL)', (key, svr, buf))
        ins += 1
    m.commit()
    print('Media 行：插入 %d，已存在跳过 %d，无音频 %d；现有 %d 行'
          % (ins, skip, nomedia, m.execute('select count(*) from Media').fetchone()[0]))

    # (2) MSG 行的 XML：对齐客户端自己写出来的形状（**不碰 MediaInfo**）
    nlen = 0
    for lid, _t, _svr, _ct, _sq in rows:
        buf = audio.get(lid)
        sc = c.execute('select StrContent from MSG where localId=?', (lid,)).fetchone()[0]
        new = patch_xml(sc, lid, buf, args.fix_length)
        if args.fix_length and buf and xml_length(sc) != expected_length(buf):
            nlen += 1
        c.execute('update MSG set MsgServerSeq=1, Reserved2=NULL, Reserved3=NULL, '
                  "DisplayContent='', StrContent=? where localId=?", (new, lid))
    c.commit()
    print("MSG 行：对齐 %d 条（MsgServerSeq=1 / Reserved2,3=NULL / DisplayContent='' / "
          'bufid=0 / aeskey+voiceurl+voicemd5 / silklength）' % len(rows))
    if args.fix_length:
        print('       顺带修 length= 的 %d 条（已用 (?<![a-zA-Z])length= 避免误伤 voicelength）' % nlen)
    print('integrity: MSG=%s  Media=%s' % (_wxcom.integrity(plain_msg), _wxcom.integrity(plain_media)))
    c.close()
    m.close()

    for plain, inst, key, tag in ((plain_msg, msg_db, msg_key(args), 'MSG0'),
                                  (plain_media, media_db, media_key(args), 'MediaMSG0')):
        out = os.path.join(args.work, '%s_voice.db' % tag)
        # 日志模式必须显式设回 WAL(2/2)：`decrypt_db` 把明文页头第 18/19 字节归一成 1/1，
        # 而 `encrypt_db` 不会替我们改回来 —— 漏了这一步会**静默把客户端的 WAL 降级成
        # rollback**（3.x 自己写的库全是 `10 00 02 02`）。这两个字节在 page 0 的密文里，
        # 所以必须在加密**之前**、在明文上改。
        mode = _need('set_journal_mode').set_journal_mode(plain, wal=True)
        print('%-10s 日志模式：%d/%d（WAL）' % (tag, mode, mode))
        hp = _wxcom.truncate_to_header(plain)
        print('%-10s 按头部页数截断：%d 页' % (tag, hp))
        npg = _wxcom.encrypt_db(plain, out, key, _wxcom.read_salt(inst))
        print('加密 %-10s %s（%d 页，%d B）' % (tag, out, npg, os.path.getsize(out)))
    return 0


# ------------------------------------------------------------------ verify
def cmd_verify(args):
    _need('decrypt_db', 'integrity', 'usable_diff')
    wx = _wxcom
    _acct_root, _msg_db, _media_db = paths_of(args)
    out_msg = os.path.join(args.work, 'MSG0_voice.db')
    out_media = os.path.join(args.work, 'MediaMSG0_voice.db')
    plain_msg = os.path.join(args.work, '_plain_voice_msg.db')
    plain_media = os.path.join(args.work, '_plain_voice_media.db')
    for f in (out_msg, out_media):
        if not os.path.exists(f):
            raise SystemExit('先跑 build：找不到 %s' % f)

    bad_total = diff_total = 0
    tmps = []
    for out, plain, key in ((out_msg, plain_msg, msg_key(args)),
                            (out_media, plain_media, media_key(args))):
        b, bad, npages = wx.decrypt_db(out, key)
        tmp = out + '.roundtrip'
        with open(tmp, 'wb') as f:
            f.write(b)
        tmps.append(tmp)
        # 重新加密必然换新 IV，所以只比可用区；这一步交给共享库
        diff = wx.usable_diff(tmp, plain)
        print('%-12s HMAC 不符页=%d/%d  可用区不一致页=%d  integrity=%s'
              % (os.path.basename(out), bad, npages, diff, wx.integrity(tmp)))
        bad_total += bad
        diff_total += diff

    c = sqlite3.connect(tmps[0])
    m = sqlite3.connect(tmps[1])
    audio = archive_audio(args.archive_media)
    md = media_rows(m)
    ok = miss = badmagic = lenbad = 0
    for lid, _t, svr, _ct, _sq in voice_rows(c):
        buf = audio.get(lid)
        if not buf:
            continue
        r = md.get(str(KEY_BIAS + lid))
        if not (r and r[1] == svr and r[2] == len(buf)):
            miss += 1
            continue
        ok += 1
        if codec_of(r[3]) == 'UNKNOWN':
            badmagic += 1
        sc = c.execute('select StrContent from MSG where localId=?', (lid,)).fetchone()[0]
        xl, exp = xml_length(sc), expected_length(buf)
        if xl is not None and exp is not None and xl != exp:
            lenbad += 1
    print('① Media 逐行核对（Key / Reserved0 / Buf 长度）：匹配 %d，不符 %d' % (ok, miss))
    print('   音频魔数不是 SILK/AMR 的：%d' % badmagic)
    print('   XML length= 与音频不一致的：%d' % lenbad)
    c.close()
    m.close()
    for t in tmps:
        if os.path.exists(t):
            os.remove(t)
    good = bad_total == 0 and diff_total == 0 and miss == 0 and badmagic == 0
    print('VERIFY:', 'PASS' if good else 'FAIL')
    if lenbad and not args.fix_length:
        print('（length= 不一致只是元数据，不影响播放；要修就加 --fix-length 重跑 build）')
    return 0 if good else 1


# ------------------------------------------------------------------ install
def cmd_install(args):
    _acct_root, msg_db, media_db = paths_of(args)
    if cmd_verify(args) != 0:
        raise SystemExit('校验未通过，拒绝安装')
    wx = _need('backup_and_install')
    bdir = args.backup_dir or os.path.join(args.work, 'pre_install_voice')
    # 共享库的守卫式安装：目标旁边若有非零 -wal 就拒绝（客户端没干净退出），
    # 先备份原库（含 -wal/-shm），并打印还原命令。
    wx.backup_and_install(os.path.join(args.work, 'MSG0_voice.db'), msg_db, bdir, label='MSG')
    wx.backup_and_install(os.path.join(args.work, 'MediaMSG0_voice.db'), media_db, bdir,
                          label='MediaMSG')
    print('\n完成。但请注意：**如果 Start Time 没有前移，客户端仍然不会用这些本地音频。**')
    print('       先跑 wxstart.py 检查/修正 DBInfo，再启动客户端。')
    return 0


# ------------------------------------------------------------------ CLI
ACTIONS = {'check': cmd_check, 'build': cmd_build,
           'verify': cmd_verify, 'install': cmd_install}


def build_parser():
    p = argparse.ArgumentParser(prog='wxvoice.py',
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description=USAGE)
    p.add_argument('action', nargs='?', default='check',
                   choices=['check', 'build', 'verify', 'install'])
    p.add_argument('--wechat-dir', required=True, help='微信数据根（其下有 <账号> 目录）')
    p.add_argument('--account', required=True, help='账号目录名')
    p.add_argument('--msg-db', default='', help='显式指定 MSG*.db')
    p.add_argument('--media-db', default='', help='显式指定 MediaMSG*.db')
    p.add_argument('--archive-media', default='',
                   help='解密后的 2.x Media.db（语音音频来源；check/build/verify 需要）')
    p.add_argument('--key-hex', default='', help='MSG*.db 的 AES 密钥（hex）')
    p.add_argument('--key-env', default='', help='从该环境变量读 MSG*.db 密钥（优先）')
    p.add_argument('--media-key-hex', default='', help='MediaMSG*.db 的 AES 密钥（hex）')
    p.add_argument('--media-key-env', default='', help='从该环境变量读 MediaMSG*.db 密钥（优先）')
    p.add_argument('--work', default='./_wxwork', help='工作目录（默认 ./_wxwork）')
    p.add_argument('--backup-dir', default='', help='install 的备份目录')
    p.add_argument('--fix-length', action='store_true',
                   help='顺手把 XML 的 length= 改成与音频字节一致（默认不改）')
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE)
        return 0
    args = build_parser().parse_args(argv)          # --help 在这里也会退出 0
    if args.action in ('check', 'build', 'verify') and not args.archive_media:
        raise SystemExit('--archive-media 是必需的（语音音频来自 2.x 的 Media.db）')
    return ACTIONS[args.action](args) or 0


if __name__ == '__main__':
    sys.exit(main())
