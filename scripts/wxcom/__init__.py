#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxcom —— wx-archive-migrate 的共享库。

这一份只做四件事：SQLCipher 3.x 的加解密、明文库的小工具、`DBInfo` 读写、
`BytesExtra` 的 protobuf 编解码；另外提供「账号全库快照 / 差分」与「守卫式安装」，
以及三个 CLI 共用的统一契约。

--------------------------------------------------------------------------
SQLCipher 参数（实测确认：在真实库的全部页上逐页验证过 HMAC）
--------------------------------------------------------------------------
    page_size      4096
    reserve        48          = IV 16 + HMAC-SHA1 20 + pad 12
    KDF            PBKDF2-HMAC-SHA1，64000 轮，输出 32 字节
    mac_salt       = 每个库文件头 16 字节的 salt，逐字节 XOR 0x3A
    mac_key        = PBKDF2-HMAC-SHA1(aes_key, mac_salt, 2, 32)
    每页            AES-256-CBC，IV 每页随机；HMAC 覆盖 `密文 ‖ IV ‖ le32(页号+1)`

页布局：

    page 0    : [salt 16][密文 4032][IV 16][HMAC 20][pad 12]
    page i>0  : [密文 4048]        [IV 16][HMAC 20][pad 12]
    pad 12 字节不在 HMAC 覆盖范围内，写什么都行。

**注意 AES 密钥是「派生密钥」，不是登录口令。** 它等于
`PBKDF2(登录口令, salt, 64000)`，而 salt 是每个库文件自己的前 16 字节 ——
所以「一个库一把密钥」。它不是口令，也不要试图用口令直接解密。

--------------------------------------------------------------------------
四个必须知道的坑
--------------------------------------------------------------------------
1. **3.x 的明文页头是 `10 00 02 02`（WAL 模式），2.x 是 `10 00 01 01`。**
   扫描/匹配派生密钥时若用 `01 01` 当判据，会漏掉**所有** 3.x 的库。
2. **日志模式字节（页头第 18/19 字节）在 page 0 的密文里，本库不改它。**
   `decrypt_db()` 只把 page 0 的 `[0:16]` 从 salt 换回 SQLite magic，**原样保留** 18/19 ——
   所以从明文里读到的是库真实的值（3.x `02 02` = WAL，2.x `01 01` = rollback）。
   绝不要改掉它再把改后的值当成「库的页头」打印出来，那是工具自己造的假象。
   真要强制某个模式，用 `encrypt_db(..., journal_mode=(2, 2))` —— 而且只在内存里的
   明文副本上改，不碰输入文件。
3. **HMAC 失败要分段看。** WCDB 会把库预分配到固定尺寸（见过 52,428,800 / 62,914,560 字节），
   尾部是裸零空白页，它们的 HMAC 必然不通过。**只有「头部页数之内」的失败才说明密钥不对**；
   `decrypt_db_ex()` 会把 `bad_in_header` 与 `bad_prealloc` 分开给你。
4. **保留段 48 必须在库还是「空壳」时就写进页头，事后再改必坏。**
   SQLite 核心只会写 0（没有设置保留段的 SQL 接口）。在有内容的库上把第 20 字节
   改成 48，会让每页的单元指针数组整体错位 48 字节 —— 实测立刻
   `database disk image is malformed` / `free space corruption`。
   所以「从 DDL 造 3.x 库」必须先用 `make_sqlite_shell()` 起手，再 `executescript(DDL)`。

--------------------------------------------------------------------------
发布纪律
--------------------------------------------------------------------------
本文件里**不允许**出现任何密钥、账号、wxid、个人绝对路径。密钥一律通过
`--key-hex` 或 `--key-env` 传入，salt 一律从目标库文件头读，路径一律来自命令行。
"""
import argparse
import hashlib
import hmac as hmac_mod
import json
import os
import shlex
import shutil
import sqlite3
import sys
import tempfile
import time

PAGE = 4096
RESERVE = 48
MAGIC = b'SQLite format 3\x00'
HEADER_PAGES_OFF = 28          # 头部「数据库页数」：偏移 28..32，大端
HEADER_RESERVE_OFF = 20        # 头部「每页保留字节数」
SQLCIPHER_PAYLOAD_FRACTIONS = bytes((64, 32, 32))

MODES = ('check', 'build', 'verify', 'install')

__all__ = [
    'PAGE', 'RESERVE', 'MAGIC', 'MODES',
    'require_crypto', 'derive_mac_key', 'ensure_parent',
    'aes_from_hex', 'resolve_key', 'read_salt',
    'decrypt_db', 'decrypt_db_ex', 'encrypt_db', 'journal_mode_name',
    'header_pages', 'plain_header_info', 'truncate_to_header', 'integrity', 'usable_diff',
    'make_sqlite_shell', 'set_journal_mode', 'set_sqlcipher_page_header', 'decrypt_to_plain',
    'db_info_all', 'db_info_get', 'db_info_set',
    'enc_varint', 'enc_field', 'parse', 'subtype_of', 'bytesextra_subtypes',
    'media_paths', 'rewrite', 'basename',
    'snapshot', 'diff',
    'backup_and_install',
    'add_common_args', 'add_key_args', 'add_work_args', 'run_cli',
]


# ====================================================================== 依赖
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAVE_CRYPTO = True
    _CRYPTO_ERR = None
except Exception as _e:                                        # noqa: BLE001
    Cipher = algorithms = modes = None
    _HAVE_CRYPTO = False
    _CRYPTO_ERR = _e


def require_crypto():
    """用到 AES 的地方先调这个；缺依赖时给一条能照做的提示，而不是 ImportError 堆栈。"""
    if not _HAVE_CRYPTO:
        raise SystemExit(
            '缺少依赖 cryptography（用到 hazmat.primitives.ciphers）。\n'
            '  安装：python3 -m pip install cryptography\n'
            '  原始导入错误：%r' % (_CRYPTO_ERR,))


# ====================================================================== 密钥
def ensure_parent(path):
    """确保 `path` 的父目录存在 —— 写文件之前先调，别让读者撞上 FileNotFoundError。"""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    return path


def aes_from_hex(text, what='密钥'):
    """把 64 位十六进制字符串变成 32 字节 AES 密钥。"""
    s = (text or '').strip()
    s = s[2:] if s[:2].lower() == '0x' else s
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        raise SystemExit('%s 不是合法的十六进制字符串（长度 %d，应为 64）' % (what, len(s)))
    if len(raw) != 32:
        raise SystemExit('%s 解出来是 %d 字节，应为 32 字节（64 位 hex）' % (what, len(raw)))
    return raw


def resolve_key(args):
    """按 `--key-env` > `--key-hex` 的顺序取密钥；都没有则报错退出。

    推荐 `--key-env`：十六进制密钥写进命令行会留在 shell 历史里。
    """
    env_name = getattr(args, 'key_env', None)
    if env_name:
        val = os.environ.get(env_name)
        if not val:
            raise SystemExit('环境变量 %s 为空或不存在' % env_name)
        return aes_from_hex(val, '环境变量 %s' % env_name)
    hexv = getattr(args, 'key_hex', None)
    if hexv:
        return aes_from_hex(hexv, '--key-hex')
    raise SystemExit('必须提供 --key-env <VAR> 或 --key-hex <AES_KEY>（32 字节 AES 派生密钥）')


def read_salt(db_path):
    """读一个 SQLCipher 库文件头 16 字节 —— 这就是该库的 salt。"""
    with open(db_path, 'rb') as f:
        salt = f.read(16)
    if len(salt) != 16:
        raise SystemExit('%s 太短，读不到 16 字节 salt' % db_path)
    return salt


def derive_mac_key(aes, salt):
    """mac_key = PBKDF2-HMAC-SHA1(aes_key, salt XOR 0x3A, 2, 32)。"""
    if len(aes) != 32:
        raise SystemExit('AES 密钥必须是 32 字节，收到 %d' % len(aes))
    if len(salt) != 16:
        raise SystemExit('salt 必须是 16 字节，收到 %d' % len(salt))
    mac_salt = bytes(x ^ 0x3A for x in salt)
    return hashlib.pbkdf2_hmac('sha1', aes, mac_salt, 2, 32)


# ====================================================================== 加解密
def decrypt_db_ex(path, aes):
    """整库解密 -> 一个带 HMAC 分类的 dict（`decrypt_db()` 的完整版）。

    明文布局与源实现一致：page 0 = `magic 16 + 明文 4032 + 保留段 48`，
    page i>0 = `明文 4048 + 保留段 48`（保留段原样带回，不做解码）。

    第 0 页**只做** `salt -> 'SQLite format 3\\0'` 的 16 字节替换，**不动**第 18/19
    字节 —— 日志模式原样保留，所以从 `plain` 里读到的就是库里真实的值
    （3.x 是 `02 02` = WAL，2.x 是 `01 01` = rollback）。工具绝不能把这个值改掉再
    打印出来当成「库的页头」，那是工具自己造的假象。

    返回：

        plain           明文 bytes
        npages          文件里的总页数
        bad_total       HMAC 不通过的页数（合计）
        bad_pages       这些页的页号列表
        header_pages    明文头部声明的页数（0 或 > npages 视为不可信，返回 0）
        bad_in_header   **只有这一段才说明密钥对不对**
        bad_prealloc    预分配区（超出头部页数的那部分）的失败数。WCDB 会把库预分配到
                        固定尺寸，尾部是裸零空白页，它们的 HMAC 必然不通过 ——
                        **这不是密钥问题**，别据此下「密钥不对」的结论。
    """
    require_crypto()
    if not os.path.exists(path):
        raise SystemExit('库不存在：%s' % path)
    blob = open(path, 'rb').read()
    if len(blob) < PAGE:
        raise SystemExit('%s 只有 %d 字节，不足一页' % (path, len(blob)))
    if len(blob) % PAGE:
        raise SystemExit('%s 大小 %d 不是 %d 的整数倍 —— 不是 SQLCipher 库？'
                         % (path, len(blob), PAGE))
    salt = blob[:16]
    mac_key = derive_mac_key(aes, salt)
    npages = len(blob) // PAGE
    iv_off = PAGE - RESERVE                 # 4048
    mac_off = PAGE - 32                     # 4064
    out = bytearray()
    bad_pages = []
    for i in range(npages):
        pg = blob[i * PAGE:(i + 1) * PAGE]
        ct_iv = pg[16:mac_off] if i == 0 else pg[0:mac_off]
        want = hmac_mod.new(mac_key, ct_iv + (i + 1).to_bytes(4, 'little'),
                            hashlib.sha1).digest()
        if want != pg[mac_off:mac_off + 20]:
            bad_pages.append(i)
        iv = pg[iv_off:iv_off + 16]
        ct = pg[16:iv_off] if i == 0 else pg[0:iv_off]
        pt = Cipher(algorithms.AES(aes), modes.CBC(iv)).decryptor().update(ct)
        out += (salt + pt) if i == 0 else pt
        out += pg[iv_off:]                  # 保留段原样带回
    out[0:16] = MAGIC                       # 只换 magic，不碰 18/19
    header_pages = int.from_bytes(out[HEADER_PAGES_OFF:HEADER_PAGES_OFF + 4], 'big')
    trustworthy = 0 < header_pages <= npages
    if trustworthy:
        bad_in_header = sum(1 for i in bad_pages if i < header_pages)
    else:
        bad_in_header = len(bad_pages)      # 头部页数不可信时保守处理
    return {
        'plain': bytes(out),
        'npages': npages,
        'bad_pages': bad_pages,
        'bad_total': len(bad_pages),
        'header_pages': header_pages if trustworthy else 0,
        'bad_in_header': bad_in_header,
        'bad_prealloc': len(bad_pages) - bad_in_header,
    }


def decrypt_db(path, aes):
    """整库解密 -> `(明文 bytes, HMAC 不通过的页数, 总页数)`。

    这是 `decrypt_db_ex()` 的薄封装。注意 `bad` 是**合计**：WCDB 预分配的尾部空白页
    的 HMAC 必然不通过。要判断「密钥是否正确」请看 `decrypt_db_ex()['bad_in_header']`
    —— **头部页数之内**的失败数才是判据。
    """
    info = decrypt_db_ex(path, aes)
    return info['plain'], info['bad_total'], info['npages']


def encrypt_db(plain_path, out_path, aes, salt, journal_mode=None):
    """把明文库按 SQLCipher 方案加密 -> 返回页数。salt 必须显式给出。

    明文文件的前 16 字节是 SQLite magic、不是 salt，所以 salt 只能从别处来
    （通常是**已经装在客户端里的那个库**的文件头，见 `--salt-from`）。
    沿用原 salt 是必须的：客户端会用它派生出密钥，换 salt 等于换密钥。

    `journal_mode` 默认 `None` = **不改**，原样使用明文里的日志模式字节（推荐）。
    需要强制时传二元组，例如 `(2, 2)` = WAL、`(1, 1)` = rollback journal；
    改动只发生在**内存里的明文副本**上，**不会去动 `plain_path` 这个文件**。
    """
    require_crypto()
    if len(salt) != 16:
        raise SystemExit('salt 必须是 16 字节，收到 %r' % (salt,))
    data = bytearray(open(plain_path, 'rb').read())
    if len(data) % PAGE:
        raise SystemExit('明文大小 %d 不是 %d 的整数倍' % (len(data), PAGE))
    if data[:16] != MAGIC:
        raise SystemExit('%s 不是 SQLite 明文库（magic 不符）' % plain_path)
    if journal_mode is not None:
        data[18], data[19] = journal_mode
    mac_key = derive_mac_key(aes, salt)
    npages = len(data) // PAGE
    out = bytearray()
    for i in range(npages):
        pg = data[i * PAGE:(i + 1) * PAGE]
        if i == 0:
            pt = pg[16:PAGE - RESERVE]      # 丢掉 magic
            head = salt
        else:
            pt = pg[0:PAGE - RESERVE]
            head = b''
        iv = os.urandom(16)
        ct = Cipher(algorithms.AES(aes), modes.CBC(iv)).encryptor().update(pt)
        mac = hmac_mod.new(mac_key, ct + iv + (i + 1).to_bytes(4, 'little'),
                           hashlib.sha1).digest()
        page = head + ct + iv + mac + bytes(12)
        assert len(page) == PAGE, len(page)
        out += page
    # 千万不要在密文上改版本号 —— 它在 page 0 的密文里，改了 HMAC 就不对了。
    ensure_parent(out_path)
    open(out_path, 'wb').write(bytes(out))
    return npages


# ====================================================================== 明文工具
def header_pages(path):
    """读明文库头部的「数据库页数」（偏移 28..32，大端）。"""
    with open(path, 'rb') as f:
        head = f.read(32)
    if len(head) < 32:
        raise SystemExit('%s 太短，读不到头部' % path)
    if head[:16] != MAGIC:
        raise SystemExit('%s 不是 SQLite 明文库（magic 不符）' % path)
    return int.from_bytes(head[HEADER_PAGES_OFF:HEADER_PAGES_OFF + 4], 'big')


def journal_mode_name(write_ver, read_ver):
    """日志模式的读法：**`02 02` = WAL**（3.x 的库就是这个），`01 01` = rollback journal。"""
    if (write_ver, read_ver) == (2, 2):
        return 'WAL'
    if (write_ver, read_ver) == (1, 1):
        return 'rollback'
    return '未知(%d/%d)' % (write_ver, read_ver)


def plain_header_info(plain_path):
    """读明文页头 -> dict：page_size / 保留段 / 日志模式 / 头部页数。

    这是**唯一**该被打印出来的页头来源 —— 不要打印「解密后再被工具改过」的值。
    """
    with open(plain_path, 'rb') as f:
        head = f.read(32)
    if len(head) < 32:
        raise SystemExit('%s 太短，读不到头部' % plain_path)
    if head[:16] != MAGIC:
        raise SystemExit('%s 不是 SQLite 明文库（magic 不符）' % plain_path)
    ps = int.from_bytes(head[16:18], 'big')
    return {
        'hex': head[16:24].hex(' '),
        'page_size': ps if ps not in (0, 1) else 65536,
        'reserve': head[20],
        'write_ver': head[18],
        'read_ver': head[19],
        'mode': journal_mode_name(head[18], head[19]),
        'header_pages': int.from_bytes(head[HEADER_PAGES_OFF:HEADER_PAGES_OFF + 4], 'big'),
        'payload_fractions': list(head[21:24]),
    }


def truncate_to_header(plain_path):
    """按头部页数截断明文，返回页数。

    必须做：客户端的 WCDB 会把库预分配到固定大小（见过 52,428,800 与 62,914,560 字节），
    尾部是裸零的空白页。连这些空白页一起加密写回去，等于把预分配尺寸固化成真实数据。
    """
    npages = header_pages(plain_path)
    if npages <= 0:
        raise SystemExit('头部页数 %d 不合理' % npages)
    want = npages * PAGE
    size = os.path.getsize(plain_path)
    if size < want:
        raise SystemExit('文件 %d 字节小于头部声明的 %d 字节，明文已被截断？' % (size, want))
    if size > want:
        with open(plain_path, 'r+b') as f:
            f.truncate(want)
    return npages


def integrity(plain_path):
    """`pragma integrity_check` 的第一行；正常是 `'ok'`。"""
    try:
        c = sqlite3.connect('file:%s?mode=ro' % plain_path, uri=True)
        try:
            return c.execute('pragma integrity_check').fetchone()[0]
        finally:
            c.close()
    except Exception as e:                                     # noqa: BLE001
        return 'ERROR: %r' % (e,)


def usable_diff(path_a, path_b, ignore_version_bytes=True):
    """只比对 SQLite 真正读取的可用区 `[0, PAGE-RESERVE)`，返回不一致的页数。

    重新加密必然换新 IV（每页随机），所以整页比对永远不等；
    有用的判据是「可用区逐字节一致 + 全页 HMAC 通过」。

    `ignore_version_bytes` 默认跳过 page 0 的第 18/19 字节：调用方可能在
    `encrypt_db(journal_mode=...)` 里**显式**要求换一个日志模式，那是有意为之的差异，
    不该算进「可用区不一致」。日志模式本身请单独用 `plain_header_info()` 报告。
    """
    a = open(path_a, 'rb').read() if isinstance(path_a, str) else bytes(path_a)
    b = open(path_b, 'rb').read() if isinstance(path_b, str) else bytes(path_b)
    a = bytearray(a)
    b = bytearray(b)
    if ignore_version_bytes and len(a) >= 20 and len(b) >= 20:
        a[18] = b[18] = 0
        a[19] = b[19] = 0
    usable = PAGE - RESERVE
    n = 0
    for i in range(0, min(len(a), len(b)), PAGE):
        if a[i:i + usable] != b[i:i + usable]:
            n += 1
    if len(a) != len(b):
        n += 1
    return n


def set_journal_mode(plain_path, wal=True):
    """**显式**把明文 page 0 的第 18/19 字节设成 WAL(2/2) 或 rollback(1/1)。

    这是一个**可选**动作：`decrypt_db()` 不会改这两个字节，所以从客户端库里解出来的
    明文本来就已经是它真实的模式（3.x 是 `02 02`）。只有两种情况需要你显式调用：

      * **新建**一个 3.x 库时（`make_sqlite_shell()` 已经写成 2/2，通常不必再调）；
      * 你确实想把一个 rollback 模式的库改成 WAL（或反过来）。

    必须在**加密之前**调用，并且调用后要重新加密 —— 直接改密文会毁掉 page 0 的 HMAC。
    若只是想在加密产物里改模式，用 `encrypt_db(..., journal_mode=(2, 2))` 更干净：
    它只在内存里改，不会动这个文件。
    """
    val = 2 if wal else 1
    if header_pages(plain_path) < 1:
        raise SystemExit('空库？')
    with open(plain_path, 'r+b') as f:
        f.seek(18)
        f.write(bytes([val, val]))
    return val


def make_sqlite_shell(path, page_size=PAGE, reserve=RESERVE, wal=True):
    """造一个「1 页、0 张表、保留段 = reserve」的合法 SQLite 库文件。

    ## 为什么必须这样起手（实测确认，踩过才知道）

    SQLCipher 的页布局是「可用区 `page_size - reserve` ＋ 保留段 48」，而头部第 20 字节
    必须声明 48 才自洽。可是 **SQLite 核心只会写 0** —— 它没有「设置保留段」的 SQL 接口
    （SQLCipher 走的是 `SQLITE_FCNTL_RESERVE_BYTES`）。于是只剩两条路：

    * **在已有内容的库上事后改第 20 字节 = 48**：不行。SQLite 是按 usable=4096 排的页，
      一旦读成 usable=4048，每页的**单元指针数组位置整体错 48 字节**，立刻
      `database disk image is malformed` / `free space corruption`（实测）。
    * **先造一个空壳，让 SQLite 一开库就从页头读到 48**：可行。壳只有 1 页、
      `sqlite_master` 里 0 行（单元格数 0），所以指针数组没有内容可以错位；
      SQLite 之后建表、插行都会按 usable = 4096-48 = 4048 布局（实测 3000 行
      `integrity_check = ok`，加解密往返也一致）。

    所以「从 DDL 现造 3.x 库」的正确起手是：先 `make_sqlite_shell()`，再
    `executescript(DDL)`。这样既不必依赖别人的模板库，也不会造出坏库。
    """
    usable = page_size - reserve
    if usable < 480:
        raise SystemExit('reserve=%d 太大：可用区只剩 %d 字节' % (reserve, usable))
    h = bytearray(100)
    h[0:16] = MAGIC
    h[16:18] = page_size.to_bytes(2, 'big')
    h[18] = 2 if wal else 1                     # 写版本
    h[19] = 2 if wal else 1                     # 读版本（3.x 是 2/2 = WAL）
    h[20] = reserve
    h[21:24] = SQLCIPHER_PAYLOAD_FRACTIONS
    h[24:28] = (1).to_bytes(4, 'big')           # 文件变更计数
    h[28:32] = (1).to_bytes(4, 'big')           # 页数 = 1
    h[32:36] = (0).to_bytes(4, 'big')           # 空闲链首
    h[36:40] = (0).to_bytes(4, 'big')           # 空闲页数
    h[40:44] = (0).to_bytes(4, 'big')           # schema cookie
    h[44:48] = (4).to_bytes(4, 'big')           # schema 格式 = 4
    h[48:52] = (0).to_bytes(4, 'big')           # 默认缓存大小
    h[52:56] = (0).to_bytes(4, 'big')           # 最大根 btree 页
    h[56:60] = (1).to_bytes(4, 'big')           # 文本编码 = UTF-8
    h[60:64] = (0).to_bytes(4, 'big')           # user version
    h[64:68] = (0).to_bytes(4, 'big')           # 增量 vacuum
    h[68:72] = (0).to_bytes(4, 'big')           # application id
    h[92:96] = (1).to_bytes(4, 'big')           # version-valid-for
    h[96:100] = (3040000).to_bytes(4, 'big')    # 写头的 SQLite 版本号
    body = bytearray(page_size)
    body[0:100] = h
    body[100] = 0x0D                            # 叶子表 b-tree 页
    body[101:103] = (0).to_bytes(2, 'big')      # 首个空闲块
    body[103:105] = (0).to_bytes(2, 'big')      # 单元格数 = 0
    body[105:107] = usable.to_bytes(2, 'big')   # 单元格内容区起点
    body[107] = 0                               # 碎片字节
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, 'wb') as f:
        f.write(bytes(body))
    return path


def set_sqlcipher_page_header(plain_path, reserve=RESERVE):
    """**危险操作**：直接改明文头部的保留段字节。

    只在「1 页、0 张表」的库上安全（也就是刚由 `make_sqlite_shell()` 造出来的壳）。
    库里一旦有了表/行，页内布局就是按旧的 usable 排好的，改这个字节会让每页的
    单元指针数组整体错位 —— 实测直接得到 `database disk image is malformed`。
    所以这里**主动拒绝**非空壳的库，把「静默造坏库」变成一个明确的报错。
    """
    with open(plain_path, 'rb') as f:
        head = f.read(100)
    if head[:16] != MAGIC:
        raise SystemExit('%s 不是 SQLite 明文库（magic 不符）' % plain_path)
    page_size = int.from_bytes(head[16:18], 'big') or PAGE
    if os.path.getsize(plain_path) != page_size:
        raise SystemExit(
            '拒绝改 %s：它已经有 %d 页、不是空壳。\n'
            '  在已有内容的库上改保留段会让每页的单元指针数组错位 48 字节，库立刻损坏。\n'
            '  请改用 wxcom.make_sqlite_shell() 起手，再建表灌数据。'
            % (plain_path, os.path.getsize(plain_path) // page_size))
    if head[103:105] != b'\x00\x00':
        raise SystemExit('拒绝改 %s：page 1 上已经有过表（单元格数非 0）' % plain_path)
    with open(plain_path, 'r+b') as f:
        f.seek(HEADER_RESERVE_OFF)
        f.write(bytes([reserve]) + SQLCIPHER_PAYLOAD_FRACTIONS)
    return reserve


def decrypt_to_plain(db_path, aes, out_path):
    """解密 -> 写明文文件；返回 `(明文路径, HMAC 不通过页数, 总页数)`。"""
    plain, bad, npages = decrypt_db(db_path, aes)
    ensure_parent(out_path)
    with open(out_path, 'wb') as f:
        f.write(plain)
    return out_path, bad, npages


# ====================================================================== DBInfo
def db_info_all(conn):
    """`DBInfo` 的全部行，按 tableIndex 排序。"""
    return list(conn.execute('select tableIndex, tableVersion, tableDesc '
                             'from DBInfo order by tableIndex'))


def db_info_get(conn, table_index=1):
    """取一行 `DBInfo` -> `(tableIndex, tableVersion, tableDesc)` 或 None。

    这个表的两个已知行（都是客户端自己写的）：
        tableIndex=1  tableDesc='Start Time'           tableVersion = 毫秒时间戳
        tableIndex=2  tableDesc='Prefix LocalId Index'  tableVersion = 1（索引版本）

    `Start Time` 是「本分片纪元」，决定语音能不能播 —— 见 `wxstart.py`。
    """
    row = conn.execute('select tableIndex, tableVersion, tableDesc from DBInfo '
                       'where tableIndex=?', (table_index,)).fetchone()
    return tuple(row) if row else None


def db_info_set(conn, value, table_index=1, desc=None):
    """写 `DBInfo.tableVersion`；行不存在则插入（此时必须给 desc）。"""
    if db_info_get(conn, table_index) is None:
        if desc is None:
            raise SystemExit('DBInfo 里没有 tableIndex=%d，插入时必须给 desc' % table_index)
        conn.execute('insert into DBInfo(tableIndex, tableVersion, tableDesc) values(?,?,?)',
                     (table_index, value, desc))
    else:
        conn.execute('update DBInfo set tableVersion=? where tableIndex=?',
                     (value, table_index))
        if desc is not None:
            conn.execute('update DBInfo set tableDesc=? where tableIndex=?',
                         (desc, table_index))
    conn.commit()
    return db_info_get(conn, table_index)


# ====================================================================== protobuf
def enc_varint(n):
    """protobuf varint 编码。"""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def enc_field(field, wiretype, value):
    """编码一个 protobuf 字段。`wiretype=2` 时 value 是 bytes，`=0` 时是 int。"""
    head = enc_varint((field << 3) | wiretype)
    if wiretype == 2:
        return head + enc_varint(len(value)) + bytes(value)
    if wiretype == 0:
        return head + enc_varint(value)
    return head + bytes(value)


def parse(buf):
    """解一层 protobuf -> `[(字段号, wiretype, 值)]`；wt2 是 bytes、wt0 是 int。

    遇到不认识的 wiretype 或越界就**在当前位置停下并返回已有结果** ——
    这样半个包也能看清前面解出了什么，便于诊断。
    """
    i = 0
    n = len(buf)
    out = []
    while i < n:
        tag = 0
        sh = 0
        while True:
            if i >= n:
                return out
            c = buf[i]
            i += 1
            tag |= (c & 0x7F) << sh
            sh += 7
            if not c & 0x80:
                break
            if sh > 63:
                return out
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v = 0
            sh = 0
            while True:
                if i >= n:
                    return out
                c = buf[i]
                i += 1
                v |= (c & 0x7F) << sh
                sh += 7
                if not c & 0x80:
                    break
                if sh > 63:
                    return out
            out.append((fn, wt, v))
        elif wt == 2:
            ln = 0
            sh = 0
            while True:
                if i >= n:
                    return out
                c = buf[i]
                i += 1
                ln |= (c & 0x7F) << sh
                sh += 7
                if not c & 0x80:
                    break
                if sh > 63:
                    return out
            if i + ln > n:
                return out
            out.append((fn, wt, buf[i:i + ln]))
            i += ln
        elif wt == 1:
            if i + 8 > n:
                return out
            out.append((fn, wt, buf[i:i + 8]))
            i += 8
        elif wt == 5:
            if i + 4 > n:
                return out
            out.append((fn, wt, buf[i:i + 4]))
            i += 4
        else:
            return out
    return out


def _inner(msg):
    """把一个子消息解成 `(子类型, 值)`；值按 wiretype 是 bytes 或 int。"""
    st = None
    val = None
    for a, w, x in parse(msg):
        if a == 1 and w == 0:
            st = x
        elif a == 2:
            val = x
    return st, val


def subtype_of(inner):
    """子消息里的子类型值（内层字段 1 的 varint）。"""
    return _inner(inner)[0]


def bytesextra_subtypes(be):
    """`BytesExtra` 里各子消息的子类型序列（按出现顺序）。

    结构实测确认：**外层字段 1 = varint 对** `{1:子类型, 2:varint}`，
    **外层字段 3 = bytes 对** `{1:子类型, 2:bytes}`。已知子类型：
    1=fromusername、2=clientmsgid、3=voicelength(毫秒)、4=路径、5=播放过标记、
    6、7=`<msgsource>`、16、32。
    """
    return [_inner(v)[0] for fn, wt, v in parse(be or b'') if fn in (1, 3) and wt == 2]


def media_paths(be):
    """`{子类型: 路径 bytes}`，只取外层字段 3 的子消息。"""
    r = {}
    for fn, wt, v in parse(be or b''):
        if fn == 3 and wt == 2:
            st, val = _inner(v)
            if st is not None and isinstance(val, (bytes, bytearray)):
                r[st] = val
    return r


def rewrite(be, new_for):
    """重建 `BytesExtra`：把外层字段 3 里子类型命中 `new_for` 的子消息换掉 payload，
    其余字段按原顺序原样重发（字节级保持）。`new_for` 形如 `{4: b'<新路径>'}`。"""
    out = b''
    for fn, wt, v in parse(be or b''):
        if fn == 3 and wt == 2:
            fields = parse(v)
            st = next((x for a, w, x in fields if a == 1 and w == 0), None)
            inner = b''
            for a, w, x in fields:
                if a == 2 and w == 2 and st in new_for:
                    x = new_for[st]
                inner += enc_field(a, w, x)
            out += enc_field(3, 2, inner)
        else:
            out += enc_field(fn, wt, v)
    return out


def basename(p):
    """按 `\\` 和 `/` 两种分隔符取文件名（WSL 下 `os.path.basename` 不认反斜杠）。"""
    if isinstance(p, (bytes, bytearray)):
        return bytes(p).replace(b'\\', b'/').rsplit(b'/', 1)[-1]
    return str(p).replace('\\', '/').rsplit('/', 1)[-1]


# ====================================================================== 快照/差分
DEFAULT_HASH_TABLES = ('MSG', 'Media', 'MediaInfo', 'ChatCRVoice')


def snapshot(root, out_path, keyed=None, hash_tables=DEFAULT_HASH_TABLES):
    """给账号目录拍一份状态快照，写成 JSON。

    记两样东西：
      * 目录下所有 `.db` / `.db-wal` / `.db-shm` 的**大小与 mtime** ——
        这一项不需要密钥，因此能直接回答「客户端刚才动了哪个库」；
      * `keyed` 里给了密钥的库的**每张表行数**，以及 `hash_tables` 里那几张表的
        **内容指纹**（行全量 repr 的 md5 前十位）—— 用来发现「行数没变但内容变了」。

    典型用法（也就是当初定位语音闸门用的方法）：让客户端做一次操作
    （点一条语音、收一条消息），退出前后各拍一次，然后 `diff()`。
    `keyed` 形如 `{'Msg/Multi/MSG0.db': <32 字节 AES 密钥>}`。
    """
    s = {'root': os.path.abspath(root),
         'when': time.strftime('%Y-%m-%d %H:%M:%S'),
         'files': {}, 'tables': {}}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in ('.git', '__pycache__')]
        for f in fn:
            if not (f.endswith('.db') or f.endswith('.db-wal') or f.endswith('.db-shm')):
                continue
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root)
            try:
                st = os.stat(p)
            except OSError:
                continue
            s['files'][rel] = {'size': st.st_size, 'mtime': int(st.st_mtime)}
    for rel, key in (keyed or {}).items():
        p = os.path.join(root, rel)
        if not os.path.exists(p):
            s['tables'][rel] = {'err': '不存在'}
            continue
        if isinstance(key, str):
            key = bytes.fromhex(key)
        try:
            info = decrypt_db_ex(p, key)
        except SystemExit as e:
            s['tables'][rel] = {'err': str(e)}
            continue
        plain = info['plain']
        # 传错密钥时 `decrypt_db` 不会抛错（只是 HMAC 大量失败），而且它**必然**把
        # `plain[0:16]` 换成 magic（那是 salt 的位置，必须换）—— 所以**不能**用 magic 判密钥。
        # 可靠判据只有两个：
        #   ① 头部页数**之内**有没有 HMAC 失败（预分配空白页失败是正常的）；
        #   ② 解出来的 page 0 里 page_size 字段是不是 4096（随机字节命中的概率 1/65536）。
        if info['bad_in_header'] or plain[16:18] != b'\x10\x00':
            s['tables'][rel] = {
                'err': '密钥可能不对（头部内 HMAC 失败 %d 页，page_size=%s）'
                       % (info['bad_in_header'], plain[16:18].hex()),
                '_bad_hmac': info['bad_total']}
            continue
        fd, tmp = tempfile.mkstemp(suffix='.db', prefix='wxsync_')
        os.close(fd)
        try:
            with open(tmp, 'wb') as f:
                f.write(plain)
            c = sqlite3.connect('file:%s?mode=ro' % tmp, uri=True)
            t = {'_pages': info['npages'], '_bad_hmac': info['bad_total']}
            try:
                for (name,) in c.execute("select name from sqlite_master where type='table'"):
                    try:
                        n = c.execute('select count(*) from "%s"' % name).fetchone()[0]
                    except sqlite3.Error:
                        continue
                    if name in hash_tables:
                        rows = list(c.execute('select * from "%s" order by 1' % name))
                        h = hashlib.md5(repr(rows).encode('utf-8', 'replace')).hexdigest()[:10]
                        t[name] = [n, h]
                    else:
                        t[name] = n
            except sqlite3.Error as e:
                t['_err'] = str(e)
            c.close()
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        s['tables'][rel] = t
    if out_path:
        d = os.path.dirname(os.path.abspath(out_path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(out_path, 'w') as f:
            json.dump(s, f, indent=1, ensure_ascii=False)
        print('快照 -> %s' % out_path)
    return s


def diff(a_json, b_json, stream=None):
    """打印两份快照的差异，返回差异条目数（0 = 完全一致）。"""
    out = stream or sys.stdout
    A = json.load(open(a_json))
    B = json.load(open(b_json))
    n = 0
    print('文件差异  (%s -> %s)' % (A.get('when'), B.get('when')), file=out)
    for rel in sorted(set(A.get('files', {})) | set(B.get('files', {}))):
        x = A.get('files', {}).get(rel)
        y = B.get('files', {}).get(rel)
        if x != y:
            n += 1
            print('   %-44s %s -> %s' % (rel, x, y), file=out)
    print('表行数 / 内容差异', file=out)
    for rel in sorted(set(A.get('tables', {})) | set(B.get('tables', {}))):
        ta = A.get('tables', {}).get(rel, {})
        tb = B.get('tables', {}).get(rel, {})
        for name in sorted(set(ta) | set(tb)):
            if ta.get(name) != tb.get(name):
                n += 1
                fa, fb = ta.get(name), tb.get(name)
                if isinstance(fa, list) and isinstance(fb, list):
                    fa = '%d行/hash%s' % (fa[0], fa[1])
                    fb = '%d行/hash%s' % (fb[0], fb[1])
                print('   %-24s %-16s %s -> %s' % (rel, name, fa, fb), file=out)
    print('差异条目数: %d' % n, file=out)
    return n


# ====================================================================== 安装
def backup_and_install(src, dst, backup_root, force=False, label='库'):
    """守卫式安装：先备份，再替换。返回备份文件路径。

    三道闸：
      1. 目标旁边若有**非零**的 `-wal`，说明客户端没有干净退出（托盘右键 → 退出微信 时
         SQLite 会自己检查点并删掉 -wal/-shm）。直接替换会静默丢掉未检查点的写入
         —— 实测见过 WAL 里躺着一条真实新消息。要强行继续必须显式 `--force`。
      2. 备份原库（含 -wal/-shm）到 `backup_root/<时间戳>/`。
      3. 打印备份路径与**还原命令**，让回滚不需要再想。
    """
    for p, what in ((src, '待安装的文件'),):
        if not os.path.exists(p):
            raise SystemExit('%s不存在：%s' % (what, p))
    wal = dst + '-wal'
    if os.path.exists(wal):
        size = os.path.getsize(wal)
        if size and not force:
            raise SystemExit(
                '%s 旁边有 %d 字节的 -wal，说明客户端没有干净退出。\n'
                '  请先在托盘右键 →「退出微信」，等它自己检查点并删掉 -wal，再重试。\n'
                '  确认这些写入可以丢弃时才加 --force。' % (dst, size))
    stamp = time.strftime('%Y%m%d_%H%M%S')
    bdir = os.path.join(backup_root, stamp)
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, os.path.basename(dst))
    if os.path.exists(dst):
        shutil.copy2(dst, bak)
        for suf in ('-wal', '-shm'):
            if os.path.exists(dst + suf):
                shutil.copy2(dst + suf, bak + suf)
    else:
        bak = None
    for suf in ('-wal', '-shm'):
        if os.path.exists(dst + suf):
            os.remove(dst + suf)
    shutil.copy2(src, dst)
    print('安装 %s -> %s (%d B)' % (src, dst, os.path.getsize(dst)))
    if bak:
        print('备份   %s' % bak)
        print('还原   cp -p %s %s' % (shlex.quote(bak), shlex.quote(dst)))
        print('       （备份目录里若有 -wal/-shm 也一并拷回）')
    else:
        print('注意   目标原先不存在，没有产生备份')
    return bak


# ====================================================================== CLI 契约
def add_key_args(ap, with_salt=True):
    g = ap.add_argument_group('密钥')
    g.add_argument('--key-hex', metavar='<AES_KEY>',
                   help='32 字节 AES 派生密钥的十六进制（64 位 hex）')
    g.add_argument('--key-env', metavar='VAR',
                   help='从环境变量读取同一串十六进制（推荐：不会留在 shell 历史里）')
    if with_salt:
        g.add_argument('--salt-from', metavar='<DB>',
                       help='加密时沿用这个已安装库文件头 16 字节作为 salt（必须沿用原 salt）')
    return g


def add_work_args(ap, default='./_wxwork'):
    g = ap.add_argument_group('路径')
    g.add_argument('--work', metavar='DIR', default=default,
                   help='工作目录：中间产物与 backups/ 都放这里（默认 %s）' % default)
    return g


def add_common_args(ap, with_salt=True, work_default='./_wxwork'):
    add_work_args(ap, work_default)
    add_key_args(ap, with_salt)
    return ap


def run_cli(prog, doc, handlers, build_args, argv=None):
    """三个脚本共用的 CLI 契约。

        <prog>                 -> 打印用法，退出 0
        <prog> --help          -> 同上
        <prog> check  [选项]   -> 只读，报告将要改什么（**默认动作**）
        <prog> build  [选项]   -> 生成到工作目录，不碰客户端
        <prog> verify [选项]   -> 全页 HMAC + 可用区往返 + integrity + 逐行核对
        <prog> install [选项]  -> 先备份原库再替换，并打印还原命令

    给了选项但没给动作时按 `check` 处理，这样「默认安全」不是靠自觉。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(doc.strip())
        print()
        print('用法: %s [%s] [选项]' % (os.path.basename(prog), ' | '.join(MODES)))
        print('      %s --help            # 本说明' % os.path.basename(prog))
        print('说明: 不带动作时默认执行 check（只读）；install 会先备份并打印还原命令。')
        return 0
    mode = argv.pop(0) if argv[0] in MODES else 'check'
    ap = argparse.ArgumentParser(prog='%s %s' % (os.path.basename(prog), mode),
                                 description=doc.strip().splitlines()[0])
    build_args(ap)
    args = ap.parse_args(argv)
    args.mode = mode
    fn = handlers.get(mode)
    if fn is None:
        raise SystemExit('未知动作：%s' % mode)
    rc = fn(args)
    return 0 if rc is None else int(rc)
