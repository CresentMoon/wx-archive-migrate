-- ===========================================================================
--  schema-3x.sql
--  微信 3.9.12.56 —— 消息库与媒体库的 DDL（实测导出的形状）
--
--  ### Reference DDL for the WeChat 3.9.x message / media databases
--
--  这份 DDL 的来源：全部从这三个库的 `sqlite_master` **原样读出**（只做了空白归一化）。
--  其中 MSG0.db 取自**客户端新建的空库**；MediaMSG0.db 与 Media.db 取自本机运行中的库，
--  但表结构同样是客户端自己建的。也就是说 —— 这是**客户端建出来的形状**，
--  不是我们猜的、也不是从别处抄的（空库上 `Media` / `MediaInfo` / `ChatCRVoice` 都是 0 行，
--  客户端建了它们，只是按需才写）。
--
--  覆盖三个库（3.x 把它们分开了，2.x 是一个 `Msg\ChatMsg.db`）：
--      A. <DATA_ROOT>\<ACCOUNT>\Msg\Multi\MSG0.db        消息主库
--      B. <DATA_ROOT>\<ACCOUNT>\Msg\Multi\MediaMSG0.db   语音媒体
--      C. <DATA_ROOT>\<ACCOUNT>\Msg\Media.db             「聊天记录 / 收藏」媒体
--
--  怎么用：
--      1. 建库时 page_size 必须是 **4096**（SQLCipher 参数见 sqlcipher-params.md）。
--         sqlite3 里：  PRAGMA page_size = 4096;      （要在建第一张表之前设）
--      2. 按 A → B → C 依次建立。
--      3. **DBInfo 的两行种子必须自己写**，见 A 段末尾 —— 尤其是 `Start Time`。
--      4. 导入完成后按 D 段验收，再走 sqlcipher-params.md 的加密与验证流程。
--
--  ⚠️ **下面三段属于三个不同的数据库文件，必须分别建在三个库里。不要合并到一个库。**
--     原因：SQLite 的索引名是**库级全局**且**大小写不敏感**，而
--     A 段 MSGTrans 上的 `talkerIDIdx` 与 C 段 MediaInfo 上的 `TalkerIdIdx`
--     实际上是**同一个名字** —— 合并执行会在最后一步报
--     `index TalkerIdIdx already exists`。
--     客户端不会撞上这个问题，只是因为它把这两张表放在两个不同的 .db 文件里。
--     （三段分别执行时，`executescript` 各自都是干净的。）
--
--  两条通用提醒：
--      * `TEXT PRIMARY KEY` 会带出一个隐式的 `sqlite_autoindex_<表>_1`，它没有 DDL，
--        不可能也不需要在 `sqlite_master` 里出现。
--      * 3.x 会按时间**分片**：除了 MSG0.db 还可能出现 MSG1.db / MediaMSG1.db /
--        FTSMSG1.db（实测：把 `Start Time` 前移之后客户端就会滚出新分片）。
--        不要把任何工具写死成只认 MSG0.db。
-- ===========================================================================


-- ===========================================================================
-- A. Msg\Multi\MSG0.db
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- DBInfo —— 库自身的元数据；只有两行，`tableIndex` 是主键
--   tableIndex = 1  tableDesc = 'Start Time'            tableVersion = 毫秒时间戳
--   tableIndex = 2  tableDesc = 'Prefix LocalId Index'  tableVersion = 1
-- 这两行是**客户端自己写的**（`Start Time` 这个字面量在客户端 DLL 里以 UTF-16 存在）。
-- ---------------------------------------------------------------------------
CREATE TABLE DBInfo (
    tableIndex   INTEGER PRIMARY KEY,
    tableVersion INTERGER,          -- 注意：客户端建表时拼错了，就是 INTERGER
    tableDesc    TEXT
);
CREATE INDEX versionIdx ON DBInfo(tableIndex);

-- ---------------------------------------------------------------------------
-- DBInfo 种子数据（**两行都必须有**）
--
--   第 1 行 = 「本分片纪元」(Start Time)。它必须 <= 你导入的最早一条消息的 Sequence，
--             否则客户端会把导入的消息当成「从别处来的历史」，**跳过本地媒体**直接去
--             服务器下载 —— 表现就是语音点不动、图片转圈。完整推导见 voice-start-time.md。
--             先查一下你自己的数据：   SELECT min(Sequence) FROM MSG;
--
--             模板（把 <START_TIME_MS> 换成你核过的值）：
--                 INSERT INTO DBInfo(tableIndex, tableVersion, tableDesc)
--                 VALUES (1, <START_TIME_MS>, 'Start Time');
-- ---------------------------------------------------------------------------
INSERT INTO DBInfo(tableIndex, tableVersion, tableDesc)
VALUES (1, 1400000000000, 'Start Time');     -- 实测有效值，早于全部导入数据

INSERT INTO DBInfo(tableIndex, tableVersion, tableDesc)
VALUES (2, 1, 'Prefix LocalId Index');       -- 固定 1

-- ---------------------------------------------------------------------------
-- MSG —— 消息主表，26 列。逐列语义见 row-mapping.md
--
-- 三个最容易写错的地方（都是实测结论）：
--   * Sequence     = CreateTime * 1000（**毫秒**），不是「每会话的消息序号」。
--                    填错会让 (talkerId, sequence DESC) 的分页游标错乱 →
--                    每个会话只显示几十条，日期视图甚至把客户端卡死。
--   * TalkerId     必须等于该 StrTalker 在 Name2ID 里的 rowid。
--   * Reserved1    = 1（普通）/ 2（Type=34 语音）。
--
-- localId 是 AUTOINCREMENT：**沿用 2.x 的原值**，导入完成后把 sqlite_sequence
-- 更新到 max(localId)，否则客户端下一条新消息会和你的行撞 id。
-- ---------------------------------------------------------------------------
CREATE TABLE MSG (
    localId         INTEGER PRIMARY KEY AUTOINCREMENT,
    TalkerId        INT DEFAULT 0,
    MsgSvrID        INT,
    Type            INT,
    SubType         INT,
    IsSender        INT,
    CreateTime      INT,
    Sequence        INT DEFAULT 0,
    StatusEx        INT DEFAULT 0,
    FlagEx          INT,
    Status          INT,
    MsgServerSeq    INT,
    MsgSequence     INT,
    StrTalker       TEXT,
    StrContent      TEXT,
    DisplayContent  TEXT,
    Reserved0       INT DEFAULT 0,
    Reserved1       INT DEFAULT 0,
    Reserved2       INT DEFAULT 0,
    Reserved3       INT DEFAULT 0,
    Reserved4       TEXT,
    Reserved5       TEXT,
    Reserved6       TEXT,
    CompressContent BLOB,
    BytesExtra      BLOB,
    BytesTrans      BLOB
);

-- 会话列表 / 分页游标走这条索引 —— Sequence 语义错了就是从这里炸的
CREATE INDEX MsgTalkerIdSeqIndex     ON MSG(talkerId, sequence DESC);
-- 按类型过滤（图片/文件/语音等标签页）
CREATE INDEX MsgTalkerIdTypeSeqIndex ON MSG(talkerId, type, sequence DESC);
-- 按服务端 id 查找
CREATE INDEX SvrIdIndex              ON MSG(MsgSvrID);

-- ---------------------------------------------------------------------------
-- Name2ID —— 会话名 → TalkerId 的映射。**`rowid` 就是 `MSG.TalkerId`**
-- （表结构两代相同，直接从 2.x 原样搬运，包含 rowid。）
-- 自洽性校验： TalkerId 在 Name2ID 里查不到的行数必须是 0（见 D 段）。
-- ---------------------------------------------------------------------------
CREATE TABLE Name2ID (
    UsrName TEXT PRIMARY KEY          -- 单聊是 wxid，群聊是 <CHATROOM_ID>@chatroom
);

-- ---------------------------------------------------------------------------
-- MSGTrans —— 从 2.x 的 `TransTable` 映射而来： (msgLocalId, talkerId)。
-- 我们这批源表是空的，所以目标也是 0 行；客户端写新消息时也不会写它
-- （实测：收到一条语音只写 1 行 MSG + 1 行 Name2ID）。
-- 也就是说 3.x **不存在**隐藏的「每消息状态表」。
-- ---------------------------------------------------------------------------
CREATE TABLE MSGTrans (
    msgLocalId INTEGER PRIMARY KEY,
    talkerId   INT
);
CREATE INDEX talkerIDIdx ON MSGTrans(talkerId);

-- ---------------------------------------------------------------------------
-- sqlite_sequence —— 由 SQLite 自动创建（因为 MSG.localId 是 AUTOINCREMENT），
-- 不要手写 DDL。导入完成后执行：
--     UPDATE sqlite_sequence SET seq = (SELECT max(localId) FROM MSG) WHERE name = 'MSG';
-- ---------------------------------------------------------------------------


-- ===========================================================================
-- B. Msg\Multi\MediaMSG0.db     ——  语音媒体（不在文件系统里！）
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Media —— 一行 = 一条语音的音频本体。语义（实测确认）：
--     Key       = 2^40 + MSG.localId            （十进制字符串，例如 2^40+1473）
--     Reserved0 = MSG.MsgSvrID
--     Buf       = 音频字节，开头是 \x02#!SILK_V3（SILK）或 #!AMR（AMR）
--     Reserved1 / Reserved2 = NULL
--
-- 权威来源是**客户端自己写出来的那一行**（它给自己发出的语音写的就是这个形状），
-- 不是我们的推断。语音的坑（为什么音频在位却播不了）见 voice-start-time.md。
-- ---------------------------------------------------------------------------
CREATE TABLE Media (
    Key       TEXT PRIMARY KEY,       -- 隐含 sqlite_autoindex_Media_1
    Reserved0 INT,
    Buf       BLOB,
    Reserved1 INT,
    Reserved2 TEXT
);
CREATE INDEX MediaReserved0Idx ON Media(Reserved0);
CREATE INDEX MediaReserved1Idx ON Media(Reserved1);


-- ===========================================================================
-- C. Msg\Media.db     ——  「聊天记录 / 收藏」子系统的媒体库
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- ChatCRVoice —— 与 Media 同构，属于「聊天记录（合并转发 / 收藏）」子系统。
-- ⚠️ **不要把普通聊天的语音塞进这里。** 实测：客户端建了这张表但从不写它
--    （我们曾把 107 行搬进去，对播放毫无影响，其中 7 行还指向不存在的 localId，
--     最后整表清空）。普通语音只走 B 段的 MediaMSG0.Media。
-- ---------------------------------------------------------------------------
CREATE TABLE ChatCRVoice (
    Key       TEXT PRIMARY KEY,       -- 隐含 sqlite_autoindex_ChatCRVoice_1
    Reserved0 INT,
    Buf       BLOB,
    Reserved1 INT,
    Reserved2 TEXT
);
CREATE INDEX ChatCRVoiceReserved0Idx ON ChatCRVoice(Reserved0);
CREATE INDEX ChatCRVoiceReserved1Idx ON ChatCRVoice(Reserved1);

-- ---------------------------------------------------------------------------
-- MediaInfo —— 同样属于「聊天记录 / 收藏」子系统；客户端建表但从不写（实测 0 行）。
-- 列名来自客户端自己的建表语句；`Thumbnail` / `Detail` 都是 BLOB。
-- ⚠️ 我们踩过的坑：把 2.x 的 MediaInfo 整表搬进来、并且把**音频字节**放进
--    `Thumbnail` 列 —— 那是错的（既不属于这个子系统，列也不是那么用的）。
--    结论：**这张表留空**。详见 voice-start-time.md。
-- ---------------------------------------------------------------------------
CREATE TABLE MediaInfo (
    MsgLocalId INTEGER PRIMARY KEY,
    TalkerId   INTEGER DEFAULT 0,
    MsgType    INTEGER DEFAULT 0,
    Reserved0  INTEGER DEFAULT 0,
    Reserved1  TEXT,
    Thumbnail  BLOB,
    Detail     BLOB,
    Reserved2  INTEGER DEFAULT 0,
    Reserved3  TEXT
);
CREATE INDEX MsgTypeIdx  ON MediaInfo(MsgType);
CREATE INDEX TalkerIdIdx ON MediaInfo(TalkerId);


-- ===========================================================================
-- D. 建完之后的验收 SQL
-- ===========================================================================

-- 1) TalkerId 必须全部能在 Name2ID 里解析 —— 结果必须是 0
-- SELECT count(*) FROM MSG m
--  WHERE NOT EXISTS (SELECT 1 FROM Name2ID n WHERE n.rowid = m.TalkerId);

-- 2) Sequence 自洽 —— 结果必须是 0
-- SELECT count(*) FROM MSG WHERE Sequence != CreateTime * 1000;

-- 3) autoincrement 序列对齐（两个值必须相等）
-- SELECT max(localId) FROM MSG;
-- SELECT seq          FROM sqlite_sequence WHERE name = 'MSG';

-- 4) 语音闸门：Start Time 必须 <= 最早一条消息的 Sequence —— 结果必须是 1
-- SELECT (SELECT tableVersion FROM DBInfo WHERE tableIndex = 1)
--        <= (SELECT min(Sequence) FROM MSG);

-- 5) 规模核对（我们这批的实测值：约 3.2 万行 / 近 200 会话 / 跨度 <D1> ～ <D2>）
-- SELECT count(*), count(DISTINCT TalkerId),
--        datetime(min(CreateTime), 'unixepoch'), datetime(max(CreateTime), 'unixepoch')
--   FROM MSG;

-- 6) 加密之后再验（见 sqlcipher-params.md §7）：PRAGMA integrity_check = ok，且全页 HMAC 0 不符。
