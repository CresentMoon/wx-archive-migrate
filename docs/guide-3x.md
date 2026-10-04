# 第 2 步：把 2.x 的历史导入 3.9.12.56

> 面向已经拿到 **2.x 明文数据**（`ChatMsg.db` / `Media.db` + 媒体归档目录）与一把 **3.9 库的 AES 密钥** 的使用者：把消息、媒体与语音灌进 3.9.12.56，并让客户端自己承认它们。
>
> 本文是一份**可照着执行**的操作单：每一步都给出「做什么 → 命令 → 怎么验收」。机制推导与证据在每步末尾指向细节文档。

## 目标

把 2.x 的历史变成 3.9.12.56 能正常浏览、播放、打开的东西：

| 数据 | 落到哪 |
|---|---|
| 文字与消息行 | `Msg/Multi/MSG0.db` 的 `MSG` / `Name2ID` / `DBInfo` |
| 图片、缩略图 | `<ACCOUNT>\FileStorage\MsgAttach\<md5(StrTalker)>\Image\<YYYY-MM>\`（缩略图同层换成 `Thumb\`，**XOR 0xA0**） |
| 视频、文件、表情 | `<ACCOUNT>\FileStorage\Video\`、`...\File\<YYYY-MM>\`、`...\CustomEmotion\<名字前两位>\`（**明文**） |
| 语音 | `Msg/Multi/MediaMSG0.db` 的 `Media` 表（BLOB） |
| 时间闸门 | `MSG0.db` 的 `DBInfo.'Start Time'` ≤ 最早一条消息的 `Sequence` |

产出是一个**可回退**的安装：每次替换前先备份，并打印还原命令。

## 前提

- Windows 上已安装 **WeChat 3.9.12.56 (x86)**，能正常登录、能打开自己的数据目录。
- 2.x 的**明文** `ChatMsg.db` / `Media.db` 与媒体归档目录（来自第 1 步）。
- 3.9 的 `Msg/Multi/MSG0.db`、`MediaMSG0.db` 已存在（客户端至少启动过一次）。
- 一把你自己的 **AES 密钥（64 位 hex）**，通过环境变量传入，不写进命令行或文件。
- 操作期间客户端必须能**干净退出**（托盘右键 → 退出微信）。
- **密钥来自你自己**：你先得是账号主人，并在你自己的设备上以正常方式登录（扫码 / 密码）；
  本仓库不涉及破解加密、不涉及撞库或猜测口令、不涉及绕过认证，也不需要服务端侧的任何东西。
  **密钥的取得步骤不在本文里**（本文只讲格式、参数与迁移）。
- 占位符：`<DATA_ROOT>` = `WeChat Files` 根；`<ACCOUNT>` = 账号目录名；`<2X_PLAIN>` = 2.x 明文账号目录；`<ARCHIVE_2X>` = 2.x 原始归档账号目录。

## 步骤

### 1. 只读体检：确认库能读、能回写

```bash
export WX_MSG_KEY=<AES_KEY_HEX>       # MSG*.db 的密钥
export WX_MEDIA_KEY=<AES_KEY_HEX>     # MediaMSG*.db 的密钥
export DATA_ROOT="<DATA_ROOT>"
export ACCOUNT="<ACCOUNT>"
export MSG_DB="$DATA_ROOT/$ACCOUNT/Msg/Multi/MSG0.db"
export MEDIA_DB="$DATA_ROOT/$ACCOUNT/Msg/Multi/MediaMSG0.db"
export WORK=./_wxwork

python3 scripts/wxdec3x.py check --db "$MSG_DB" --key-env WX_MSG_KEY
```

**预期结果**：打印页数、头部页数、预分配余量、HMAC 坏页计数、`integrity`。
**怎么验收**：`integrity = ok`，且 **头部页数之内 0 页 HMAC 不符**。预分配尾页的 HMAC 失败是正常现象，不要据此认为密钥不对（见 [appendix/pitfalls.md](appendix/pitfalls.md) 第 14 条）。

### 2. 建目标库 + 行迁移（文本）

先用 `check` 只读体检，再建明文库并加密，最后逐行核对。

```bash
python3 scripts/wxrows.py check \
  --src <2X_PLAIN>/ChatMsg.db --schema docs/detail/schema-3x.sql

python3 scripts/wxrows.py build \
  --src <2X_PLAIN>/ChatMsg.db --schema docs/detail/schema-3x.sql \
  --out "$WORK/MSG0.plain.db" --enc-out "$WORK/MSG0.db" \
  --key-env WX_MSG_KEY --salt-from "$MSG_DB"

python3 scripts/wxrows.py verify \
  --src <2X_PLAIN>/ChatMsg.db --schema docs/detail/schema-3x.sql \
  --out "$WORK/MSG0.plain.db" --enc-out "$WORK/MSG0.db" \
  --key-env WX_MSG_KEY --salt-from "$MSG_DB"
```

目标库由 `docs/detail/schema-3x.sql` 现造，不依赖任何人的模板库。加密必须**沿用目标库原本的 16 字节 salt**（`--salt-from "$MSG_DB"`），客户端才能读出这个库。

**预期结果 / 怎么验收**：`verify` 报告全部通过 ——
- 行数、时间跨度、会话数与源表一致；`TalkerId` 不可解析 **0 行**；
- `Sequence != CreateTime * 1000` **0 行**；
- `sqlite_sequence` 已更新到 `max(localId)`（否则客户端下一条新消息会撞 id）；
- `integrity = ok`、头部页数内 HMAC 0 页不符。

> `Sequence = CreateTime * 1000`（毫秒）必须准确：它同时是客户端 `MsgTalkerIdSeqIndex(talkerId, sequence DESC)` 的**分页游标**；填成「每会话计数器」会让游标与「最新一条」两个键互相矛盾，症状是每个会话只显示几十条、日期视图卡死。细节见 [detail/row-mapping.md](detail/row-mapping.md)。

### 3. 媒体落位（图片 / 缩略图 / 视频 / 文件 / 表情）

按类型分开做，每类走 `build → verify → install`。**图片与缩略图是 XOR 0xA0，视频 / 文件 / 表情是明文**，工具按类型分别处理。

```bash
# (a) 图片 + 缩略图（落 MsgAttach\<md5(会话)>\Image|Thumb\<YYYY-MM>\）
python3 scripts/wxmedia.py images build \
  --wechat-dir "$DATA_ROOT" --account "$ACCOUNT" --archive <ARCHIVE_2X> \
  --msg-db "$MSG_DB" --key-env WX_MSG_KEY
python3 scripts/wxmedia.py images verify  --wechat-dir "$DATA_ROOT" --account "$ACCOUNT" ...
python3 scripts/wxmedia.py images install --wechat-dir "$DATA_ROOT" --account "$ACCOUNT" ...

# (b) 视频（FileStorage\Video\，扁平、没有月份子目录）
python3 scripts/wxmedia.py video    ... 同样的参数

# (c) 文件（仅 Type=49 且内层 <type>6</type>，落 FileStorage\File\<YYYY-MM>\）
python3 scripts/wxmedia.py files    ... 可加 --extra-root <DIR> 追加搜索根

# (d) 表情（纯文件放置，不改数据库，install 是空操作）
python3 scripts/wxmedia.py stickers ... 同样的参数
```

`--archive` 指向 2.x 归档的账号目录（媒体来源）。
**预期结果 / 怎么验收**：
- `verify` 逐行核对 `BytesExtra` 里记录的路径**所指文件确实存在**；`install` 先备份再替换 MSG 库并打印还原命令。
- 客户端里：任意旧会话的**图片与缩略图直接显示**；**视频能播放**；**文件卡片能打开**；**动画表情能显示**。

**不要**动 `Msg/Media.db` 的 `MediaInfo` 表 —— 3.9 建表但从不写，往里面搬数据没有任何效果。细节见 [detail/media-layout.md](detail/media-layout.md)。

### 4. 语音：写进 Media 表，再开时间闸门

语音不在文件系统里，在数据库里。分两步：先写 `MediaMSG0.db.Media` 行并对齐消息 XML，再让客户端认可这些消息属于「本库纪元」。

```bash
# 4a. 写 Media 行（Key = 2^40 + MSG.localId，Buf = 裸音频字节）
python3 scripts/wxvoice.py check   --wechat-dir "$DATA_ROOT" --account "$ACCOUNT" \
  --msg-db "$MSG_DB" --media-db "$MEDIA_DB" --archive-media <2X_PLAIN>/Media.db \
  --key-env WX_MSG_KEY --media-key-env WX_MEDIA_KEY
python3 scripts/wxvoice.py build   ... 同样的参数
python3 scripts/wxvoice.py verify  ... 同样的参数
python3 scripts/wxvoice.py install ... 同样的参数

# 4b. 时间闸门：把 DBInfo.'Start Time' 设到 ≤ min(Sequence)
python3 scripts/wxstart.py check   --db "$MSG_DB" --key-env WX_MSG_KEY
python3 scripts/wxstart.py build   --db "$MSG_DB" --key-env WX_MSG_KEY --auto \
  --out "$WORK/MSG0.plain.db" --enc-out "$WORK/MSG0.new.db"
python3 scripts/wxstart.py verify  --db "$MSG_DB" --key-env WX_MSG_KEY --auto
python3 scripts/wxstart.py install --db "$MSG_DB" --key-env WX_MSG_KEY --auto \
  --install-to "$MSG_DB"
```

`--auto` 会把 `DBInfo` 里 `tableIndex = 1` 的那行（`tableDesc = 'Start Time'`）设成 `min(Sequence)`。

**为什么必须做**：`Start Time` 是「本分片纪元」—— 只有 `Sequence >= StartTime` 的消息，客户端才会去查本地媒体；早于它的会被当成「从别处导入的历史」，**直接跳过本地音频去服务器下载**，而多年前的下载地址早已失效。表现是气泡显示着时长，点下去却是红色感叹号或无限转圈。细节见 [detail/voice-start-time.md](detail/voice-start-time.md)。

**预期结果 / 怎么验收**：`wxvoice verify` 逐行核对 `Key` / `Reserved0` / `Buf` 的魔数（`\x02#!SILK_V3` 或 `#!AMR`）；`wxstart check` 明确报告闸门通过。装完重启后，**任意一条旧语音都能直接播放**。

**预期副作用**：客户端随后会新建 `MSG1.db` / `MediaMSG1.db` / `FTSMSG1.db`。这是正常的多分片滚动，但后续脚本、备份与 `-wal` 检查都要覆盖**所有** `MSG*.db` / `MediaMSG*.db`，不要写死 `MSG0.db`。

### 5. 守卫式安装（客户端干净退出 + `-wal` 归零 + 备份）

所有会写客户端目录的 `install` 都用 `install_guard.sh` 包住：它等待进程**连续多轮**消失，确认所有目标库的 `-wal` 不存在或 0 字节，动手前再确认一次进程没复活；任一条不满足就**一个文件都不动**。

```bash
bash scripts/install_guard.sh --process WeChat \
  --scan-root "$DATA_ROOT/$ACCOUNT/Msg" \
  -- python3 scripts/wxrows.py install \
       --install-to "$MSG_DB" --out "$WORK/MSG0.plain.db" \
       --enc-out "$WORK/MSG0.db" --key-env WX_MSG_KEY --salt-from "$MSG_DB"
```

其余工具的 `install` 用同一个前缀：

```bash
bash scripts/install_guard.sh --scan-root "$DATA_ROOT/$ACCOUNT/Msg" -- \
  python3 scripts/wxmedia.py images install --wechat-dir "$DATA_ROOT" --account "$ACCOUNT" ...
bash scripts/install_guard.sh --scan-root "$DATA_ROOT/$ACCOUNT/Msg" -- \
  python3 scripts/wxvoice.py install --wechat-dir "$DATA_ROOT" --account "$ACCOUNT" ...
bash scripts/install_guard.sh --scan-root "$DATA_ROOT/$ACCOUNT/Msg" -- \
  python3 scripts/wxstart.py install --db "$MSG_DB" --key-env WX_MSG_KEY --auto --install-to "$MSG_DB"
```

**怎么做**：托盘右键 → 退出微信，让 SQLite 自己 checkpoint 并删掉 `-wal` / `-shm`。**不要**用任务管理器强杀 —— 未检查点的帧会被静默回滚，客户端刚收到的消息会消失且不报错。

**预期结果 / 怎么验收**：每条 `install` 都输出备份目录路径和一行 `还原： cp -p ...`；**把这些路径记下来**。装完后立刻用对应的 `check` 复核页数、HMAC、`integrity`、关键表行数。

### 6. 在客户端里可视化验收

启动 3.9.12.56，逐项确认 —— 这一步才是真正的「装好了」：

1. 左侧会话列表能看到历史会话，日期正确。
2. 点开一个旧的文字会话：消息连续、能向上翻到底，**不会只显示几十条**。
3. 用「消息记录浏览（日期）」翻某一天：**不卡死**。
4. 打开任意一条语音：**能直接播放**、能听到声音。
5. 图片正常显示，点开看大图正常；缩略图不转圈。
6. 打开一条视频：能播放。
7. 打开一个文件卡片：能打开或另存。
8. 动画表情显示；个别转圈的，翻过去等一会儿。

任一项不对，先别再乱点，对照下一节。

## 对应脚本

| 脚本 | 作用 | 关键参数 |
|---|---|---|
| `scripts/wxdec3x.py` | 用你自己的密钥加密 / 解密整库并回写；`check` 只读体检 | `--db`、`--out`、`--encrypt`、`--salt-from`、`--key-hex` / `--key-env`、`--journal-mode`、`--install-to` |
| `scripts/wxrows.py` | 2.x `ChatMsg` → 3.x `MSG` 行迁移（含 `Sequence` 规则与 `DBInfo` 种子） | `--src`、`--src-table`、`--schema`、`--out`、`--enc-out`、`--start-time`、`--key-env`、`--salt-from`、`--install-to` |
| `scripts/wxmedia.py` | 按类型把媒体放进 3.x 布局，并改写 `BytesExtra` 里的路径 | 类型 `images` / `video` / `stickers` / `files`；`--wechat-dir`、`--account`、`--archive`、`--extra-root`、`--msg-db`、`--key-env`、`--backup-dir` |
| `scripts/wxvoice.py` | 语音 → `MediaMSG0.db.Media`，并对齐消息 XML | 动作 `check` / `build` / `verify` / `install`；`--wechat-dir`、`--account`、`--msg-db`、`--media-db`、`--archive-media`、`--key-env`、`--media-key-env`、`--fix-length` |
| `scripts/wxstart.py` | 读写 `DBInfo.'Start Time'`（语音时间闸门） | `--db`、`--set <ms>`、`--auto`、`--out`、`--enc-out`、`--install-to`、`--key-env`、`--force` |
| `scripts/wxsync.py` | 账号全库 snapshot / diff，用来确认「客户端到底写了什么」 | `snap <标签> --data-root`、`--key` / `--key-env <库>=<VAR>`、`diff <前.json> <后.json>` |
| `scripts/install_guard.sh` | 守卫式安装：进程消失 + `-wal` 归零 + 动手前再确认 | `--process`、`--wal`、`--scan-root`、`--timeout`、`--stable`、`--probe`、`--dry-run` |

统一四步契约：`check`（只读，默认动作）→ `build`（写到工作目录，不碰客户端）→ `verify`（全页 HMAC + 可用区往返 + `integrity_check` + 逐行核对）→ `install`（先备份、再替换、打印还原命令）。

## 验收清单

- [ ] `wxdec3x.py check`：`integrity = ok`，头部页数之内 HMAC 0 页不符
- [ ] `wxrows.py verify`：行数 / 时间跨度 / 会话数与源表一致
- [ ] `TalkerId` 不可解析 0 行；`Sequence != CreateTime * 1000` 0 行
- [ ] `sqlite_sequence` 等于 `max(localId)`
- [ ] `wxmedia.py` 各类型 `verify`：路径所指文件全部存在
- [ ] `wxvoice.py verify`：`Key` / `Reserved0` / `Buf` 魔数逐行通过
- [ ] `wxstart.py check`：`Start Time ≤ min(Sequence)`（闸门通过）
- [ ] 每次 `install` 都拿到备份目录与还原命令
- [ ] 客户端里：会话列表正确 / 能翻到底 / 日期视图不卡 / 语音可播 / 图片、视频、文件、表情可见

## 常见失败

**1. 会话点进去只显示几十条，日期视图卡死（事件 1002，无 crash dump）**
- 原因：`Sequence` 写成了「每会话计数器」；或 `MicroMsg.db` 的 `ChatInfo.LastReadedCreateTime` 阅读锚点还停在旧量级。分页游标与「最新一条」互相矛盾，于是死循环。
- 怎么办：全库执行 `UPDATE MSG SET Sequence = CreateTime * 1000 WHERE Sequence != CreateTime * 1000;`，并按该会话 `max(Sequence)` 的那条消息重算锚点（`LastReadedSvrId` 取它的 `MsgSvrID`）。详见 [appendix/pitfalls.md](appendix/pitfalls.md) 第 1、7 条。

**2. 语音气泡有时长，点下去红色感叹号 / 无限转圈 / 问「是否重新下载」**
- 原因：`DBInfo.'Start Time'` 大于这些消息的 `Sequence` —— 客户端认为本地没有这条语音，跳过 `Media` 表直接去服务器下载，而旧地址早已失效。
- 怎么办：`wxstart.py build ... --auto` 之后 `install`，重启客户端。见 [detail/voice-start-time.md](detail/voice-start-time.md)。

**3. 装完客户端能开，但「刚收到的那条新消息」不见了**
- 原因：替换主库前客户端没有干净退出，`-wal` 里未检查点的帧被静默回滚（强杀必现，且不报错）。
- 怎么办：用备份还原；之后所有 `install` 都用 `install_guard.sh` 包住，等 `-wal` 归零再动手。硬杀过的库不要直接删 `-wal`。见 [appendix/pitfalls.md](appendix/pitfalls.md) 第 2 条。

**4. 图片 / 文件显示「已被清理」或打不开**
- 原因：`BytesExtra` 里的路径没被改写成当前机器成立的值，或文件根本没放到位；2.x 只在当年**点开 / 下载过**时才落盘，没下载过的原件任何副本都不存在。
- 怎么办：跑对应类型的 `verify` 看缺哪些，**只处理源文件确实存在的行** —— 把路径写成一个不存在的文件，气泡会从「已被清理」变成「打不开」，更糟。按文件名跨目录凑数前先比大小：「同名」不等于「同一个文件」。

**5. 表情转圈**
- 原因：本地没有该指纹的实体，客户端正在**联网按指纹取回** —— 这不是永远取不回来。
- 怎么办：打开会话、翻过去、等一会儿（往往 10 秒内）。不要逐条转发，更不要往 `business\emoticon\Persist` 里塞文件（那是索引驱动的，会被删）。详见 [appendix/pitfalls.md](appendix/pitfalls.md) 第 17 条。

**6. `verify` 报「所有页都不一致」**
- 原因：重新加密必然换新的随机 IV，逐字节比密文永远会失败 —— 这是设计如此，不是实现写错了。
- 怎么办：只比**可用区** `[0, page_size - 48)`（本版本 `4096 - 48 = 4048` 字节/页），并同时看全页 HMAC 与 `integrity`。工具已按这个口径实现。

---

> 下一步：[第 3 步：把 3.9 的数据交给 4.x →](guide-4x.md)
> 细节文档：[行映射 detail/row-mapping.md](detail/row-mapping.md)、[媒体布局 detail/media-layout.md](detail/media-layout.md)、[语音与时间闸门 detail/voice-start-time.md](detail/voice-start-time.md)、[建库 DDL detail/schema-3x.sql](detail/schema-3x.sql)、[坑检查表 appendix/pitfalls.md](appendix/pitfalls.md)
