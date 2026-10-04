# 把 2.x 的消息记录灌进 3.x：逐列语义，以及一个代价很大的坑

### Mapping the 2.x `ChatMsg` table into the 3.x `MSG` table — column by column

> 实测环境：源 = 2.0.0.37 的 `Msg\ChatMsg.db`；目标 = 3.9.12.56 的 `Msg\Multi\MSG0.db`
> 规模：源表 **约 3.2 万行**；装进客户端并跑过几轮之后库里 **约 3.2 万行**（客户端自己又写了 11 行）、
> **近 200 个会话**、时间跨度 **<D1> ～ <D2>**
> 列语义的来源：**客户端自己写出来的行**，不是从列名猜的（方法见 §4）

---

## 0. TL;DR

1. **`Sequence = CreateTime * 1000`（毫秒）**。它不是"每会话的消息序号"。填错会让
   `MsgTalkerIdSeqIndex(talkerId, sequence DESC)` 的分页游标错乱 ——
   症状是「每个会话只显示几十条」外加「日期视图把客户端卡死」（**实测确认**）。
2. **`TalkerId` 必须等于该 `StrTalker` 在 `Name2ID` 里的 `rowid`**。
   自洽性可以一行 SQL 校验（全量行零不匹配）。（**实测确认**）
3. **不知道列语义时不要猜** —— 让真客户端自己写一行，再读它的列值。
   本篇每一列的"目标值"都能追溯到这 11 行权威样例。（**实测确认**）

---

## 1. 表级映射

| 2.x（`Msg\*.db`） | 3.x（`Msg\Multi\MSG0.db`） | 处理方式 | 证据 |
|---|---|---|---|
| `ChatMsg` | `MSG` | 逐列映射，见 §2 | **实测确认** |
| `Name2ID` | `Name2ID` | 表结构相同；**`rowid` 就是 `TalkerId`**，原样搬（含 `rowid`） | **实测确认** |
| `TransTable` | `MSGTrans` | 列映射 `(msgLocalId, talkerId)`；我们这批源表是**空**的，所以目标也是 0 行 | **实测确认** |
| —（新库没有） | `DBInfo` | **必须自己写两行种子**，见 §5；漏了或写错 `Start Time` 会让语音播不了 | **实测确认** |
| —（SQLite 自建） | `sqlite_sequence` | 导入结束把值更新到 `max(localId)` | **实测确认** |

两代的**文件布局**也不同，别混淆：2.x 是 `Msg\ChatMsg.db`（单库），
3.x 是 `Msg\Multi\MSG0.db` + `FileStorage\`（媒体搬到文件系统）。
3.x 还会按时间**分片**（`MSG0.db`、`MSG1.db`…），见 §5 的注。

---

## 2. 逐列语义（`MSG` 的 26 列）

下表"目标值"一列**全部取自客户端自己写的行**（Type=1 文字、3 图片、34 语音、49 文件卡片各至少一条），
不是从列名推断出来的。

| # | 列 | 语义 / 目标值 | 证据 |
|---|---|---|---|
| 1 | `localId` | 沿用 2.x 的原值。它是 `INTEGER PRIMARY KEY AUTOINCREMENT` | **实测确认** |
| 2 | `TalkerId` | **必须等于该 `StrTalker` 在 `Name2ID` 里的 `rowid`** | **实测确认**（约 3.2 万行零不匹配） |
| 3 | `MsgSvrID` | 服务端消息 id，原样搬 | **实测确认** |
| 4 | `Type` | 消息类型，原样搬（1 文字 / 3 图片 / 34 语音 / 43·62 视频 / 47 表情 / 49 文件或链接卡片） | **实测确认** |
| 5 | `SubType` | `0`；文件类 `Type=49` 可能是 `6` | **实测确认** |
| 6 | `IsSender` | 原样搬 | **实测确认** |
| 7 | `CreateTime` | **秒级**时间戳，原样搬 | **实测确认** |
| 8 | **`Sequence`** | **`= CreateTime * 1000`（毫秒）** —— 见 §3 | **实测确认** |
| 9 | `StatusEx` | `0` | **实测确认** |
| 10 | `FlagEx` | `0` | **实测确认** |
| 11 | `Status` | `2`（我们这批；2.x 的 `Status` 原样搬也得到 2） | **实测确认** |
| 12 | `MsgServerSeq` | 语音行 = `1`（与客户端自写行同值）；其余行我们填 `0`，实测客户端工作正常 | **实测确认** |
| 13 | `MsgSequence` | 每会话计数器 1..N；3.9 给新会话播种自一个**全局**递增序列（数值量级 8~9 位数、随每条新消息递增；具体值因账号而异，本文不给） | **实测确认** |
| 14 | `StrTalker` | 会话用户名，必须是 `Name2ID` 里存在的 `UsrName`。**老账号的单聊用户名多数不是 `wxid` 形状，而是自定义名**（我们这份十年前的数据里几乎全是这种）；群聊是 `<CHATROOM_ID>@chatroom` | **实测确认** |
| 15 | `StrContent` | 正文：纯文本，或消息 XML | **实测确认** |
| 16 | `DisplayContent` | 空串 `''` | **实测确认** |
| 17 | `Reserved0` | `0` | **实测确认** |
| 18 | `Reserved1` | **`1`（普通）/ `2`（`Type=34` 语音）** | **实测确认** |
| 19–23 | `Reserved2`…`Reserved6` | `NULL`（客户端自写行也是 `NULL`） | **实测确认** |
| 24 | `CompressContent` | `NULL`（客户端自写行也是 `NULL`） | **实测确认** |
| 25 | `BytesExtra` | protobuf，装路径/时长等；结构见 `media-layout.md` | **实测确认** |
| 26 | `BytesTrans` | `NULL`（客户端自写行也是 `NULL`） | **实测确认** |

> `Reserved1` 这一列很能说明"为什么不能猜"：名字完全无语义，而它的值恰好取决于消息类型
> （普通 1、语音 2）。这种信息只能从客户端自己写的行里读出来。

---

## 3. 那个代价很大的坑：`Sequence`

### 症状

1. 每个会话点进去**只显示几十条**消息（远少于实际条数）。
2. 打开「消息记录浏览（日期）」时客户端**卡死**。

Windows 事件日志里是 **`Application Hang`，事件 ID 1002** ——
**不是** 1000（Application Error），而且**没有 crash dump**。
这个组合说明是**死循环**，不是非法内存访问。所以是数据把客户端绕进了循环。（**实测确认**）

### 根因

这批数据是**分两批**灌进去的：先灌 `localId <= <N0>`，之后再补尾巴
`localId <N0+1>..<N1>`。而两道后处理脚本里都硬编码了 `localId <= <N0>`
这个**当时以为的最大边界**，于是尾巴整体被漏掉 ——
尾巴 696 行的 `Sequence` 仍是老的"每会话计数器"值（1..253）。

为什么这会死循环：这些行的 `CreateTime` 是会话里**最新**的，但按 `Sequence` 排序却排到**最前**。
也就是——**按 `localId` 是"最后"，按 `Sequence` 是"最先"**。客户端一个键走游标
（`MsgTalkerIdSeqIndex(talkerId, sequence DESC)`）、另一个键找"最新那条"，
两个键互相矛盾，游标永远推不动。**34 个会话受影响。**（**实测确认**）

### 修法

一行就够：

```sql
UPDATE MSG SET Sequence = CreateTime * 1000 WHERE Sequence != CreateTime * 1000;
```

我们这次修了 696 行。**同一个硬编码边界还漏了另外两处**，顺手一并修掉：

| 偏离 | 行数 | 正确值 |
|---|---|---|
| `Sequence` 仍是每会话计数器 | 696 | `CreateTime * 1000` |
| `Reserved1` = 0 | 696 | `1`，语音 `2` |
| `BytesExtra` 仍指向 2.x 那台机器的**绝对路径**（`Type=3` 图片） | 44 | `<ACCOUNT>\FileStorage\MsgAttach\…` |

第三处要单独重视：日期视图会把**一整天**的消息（含图片）都渲染出来，所以哪怕只有几行路径失效，
也可能在浏览某一天时出问题。

### 一条教训 + 一个化石坑

* **教训：不要硬编码行边界。** 边界要从数据推导（`max(localId)`、或"源表全部行"），
  否则任何"补一批"的操作都会静默漏掉新尾巴。详见 `pitfalls.md`。
* **化石坑（极易误判）**：`MicroMsg.db` 的 `ChatInfo.LastReadedCreateTime` 是**阅读锚点**，
  客户端用它在 `Sequence` 轴上定位显示窗口。如果你在"`Sequence` 还是每会话计数器"的
  那次运行里写过锚点，锚点就变成 `78` / `1157` / `3537` 这种量级 ⇒ 落在 1970 年 ⇒
  这个会话只显示几十条。
  **症状极容易认错**：左侧会话列表的日期是**正确**的（那由 `Session` 表管），
  点进去却只有几十条 —— 看起来像另一个 bug。
  修法：按该会话 `max(Sequence)` 的那条消息，重算 `ChatInfo.LastReadedCreateTime`
  （`LastReadedSvrId` 取该行的 `MsgSvrID`）。

---

## 4. 方法论：不要猜列语义，让客户端自己写一行

### 为什么

3.x 的 `MSG` 表里大量列名是缩写，还有一整排 `Reserved1`…`Reserved6`。
光看名字，你猜不出 `Reserved1` 为什么是 1 或 2、`Status` 为什么是 2、
`MsgSequence` 和 `Sequence` 谁管排序。**猜错的代价就是 §3 那种返工。**

### 怎么做（四步，可照抄）

1. **让真客户端自己写一行。** 最省事的办法：用**另一个账号**给你的某个会话发消息，
   不同类型的各发一条（文字 / 图片 / 文件 / 语音）。语音最好发，因为它会顺带把媒体表也写出来。
2. 客户端**干净退出**（托盘 → 退出）之后，把库解密出来（参数与工具见 `sqlcipher-params.md`）。
   干净退出很重要：运行时数据还在 `-wal` 里（见 `pitfalls.md`）。
3. 读**新增的那一行**的全部 26 列 —— 这就是**权威样例**。对比"写入前后"两份库可以精确定位到它。
4. 把你要导入的值**对齐到它**；对不上的列就照它抄，而不是照你的直觉。

### 我们这样得到的东西

* 权威样例覆盖了 `Type` = 1 / 3 / 34 / 49，本篇 §2 的每一列都能追溯到它们。
* 副产品之一：客户端写一条新消息时只写 **1 行 `MSG` +（必要时）1 行 `Name2ID`**，
  `MSGTrans` 始终 0 行 ⇒ 3.x **不存在**隐藏的"每消息状态表"。这一条直接砍掉了一整类猜测。
* 副产品之二：客户端**会改写它自己拥有的行**。实测：成功播放一条语音之后，
  它会往那行的 `BytesExtra` 里补一个"播放过"的标记（外层字段 1、子类型 5）；
  重开一次还可能把整个库重新预分配。**所以不要假设你的字节级写入会一直保持原样**（见 `pitfalls.md`）。

> 推论：任何"3.x 这里该填什么"的问题，第一个该问的不是文档、也不是别人的实现，
> 而是**"客户端自己写的时候填什么"**。

---

## 5. 从 DDL 建目标库

**不要用别人的模板库。** 我们的第一版是拿"客户端新建的空库"当模板拷贝的 ——
那本质上是**账号数据**（里面已经有 `Name2ID`、`DBInfo` 之类），不适合随代码分发，
也不该让读者去猜它的来历。

改用 `schema-3x.sql` 建空库。三点必须注意：

1. **建库时 `page_size` 必须是 4096**（SQLCipher 参数见 `sqlcipher-params.md`）。
2. **`localId` 沿用 2.x 的原值**，导入完成后把 `sqlite_sequence` 更新到 `max(localId)`，
   否则客户端下一条新消息会和你已有的行**撞 id**。
3. **`DBInfo` 的两行种子必须自己写**，尤其是第 1 行 `Start Time`：
   它必须 **≤ 你导入的最早一条消息的 `Sequence`**。
   写大了，客户端会把导入的这些消息当成"从别处来的历史"，**跳过本地媒体**去服务器下载 ——
   表现就是语音点不动、图片转圈。这一条的完整推导见 **`voice-start-time.md`**。

建好库之后再走 `sqlcipher-params.md` 的加密与验证流程。

> 注：3.x 会按时间**分片**。我们这台机器上，把 `Start Time` 前移之后客户端又滚出了
> `MSG1.db` / `MediaMSG1.db` / `FTSMSG1.db`（**实测确认**）。这是客户端的正常行为，
> 但你的工具和后续步骤要按"多分片"写，别写死 `MSG0.db`。

---

## 6. 校验导入结果

导入完、加密前，跑这一组 SQL（全部应当给出标注的结果）：

```sql
-- 1) 行数一致（与源表比）
SELECT count(*) FROM MSG;

-- 2) 时间跨度一致
SELECT min(CreateTime), max(CreateTime) FROM MSG;

-- 3) 会话数
SELECT count(DISTINCT TalkerId) FROM MSG;

-- 4) TalkerId 全部能在 Name2ID 里解析 —— 必须是 0
SELECT count(*) FROM MSG m
 WHERE NOT EXISTS (SELECT 1 FROM Name2ID n WHERE n.rowid = m.TalkerId);

-- 5) Sequence 自洽 —— 必须是 0
SELECT count(*) FROM MSG WHERE Sequence != CreateTime * 1000;

-- 6) autoincrement 序列对齐
SELECT max(localId) FROM MSG;                       -- 应等于
SELECT seq FROM sqlite_sequence WHERE name = 'MSG';

-- 7) 语音闸门：Start Time 必须 <= 最早一条消息的 Sequence
SELECT (SELECT tableVersion FROM DBInfo WHERE tableIndex = 1)
       <= (SELECT min(Sequence) FROM MSG) AS start_time_ok;
```

加密之后再加两步（细节见 `sqlcipher-params.md` §7）：`PRAGMA integrity_check` = `ok`，
以及**全页 HMAC 0 不符**。

我们的实测结果（可作对照）：约 3.2 万行 / 近 200 会话 /
<D1> ～ <D2> / `TalkerId` 不可解析 0 行 / `Sequence` 不自洽 0 行 /
`integrity_check` = `ok` / 全页 HMAC 0 不符。（**实测确认**）

---

## 7. 免责声明

本文记录的是**文件格式与字段语义**，以及**在你自己的机器、你自己的账号、你自己的数据上**
读写这些文件的工具。不包含任何绕过他人账号保护的内容，也不提供任何形式的担保。
请自行确认当地法律与软件许可协议。**不要**把数据库、导出内容或聊天内容提交到本仓库。
