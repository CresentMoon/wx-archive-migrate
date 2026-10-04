# 语音与「时间闸门」—— 为什么语音明明在库里却播不了，以及一句话修好它

> 实测环境：WeChat PC **3.9.12.56 (x86)**，账号数据从 **2.0.0.37** 导入。
> 本文全部结论都来自**单变量对照实验**，证据强度逐条标注。

---

## 0. TL;DR

1. 3.9 的语音**不在文件系统里**，就在 `Msg/Multi/MediaMSG0.db` 的 `Media` 表里，一行一条。
   这一行的形状以**客户端自己写的那行**为准（让它自己发一条语音，然后读）。
2. 我们一开始猜错了很多东西（音频字节、容器格式、`msgsource`、`MsgSvrID`、`MsgSequence`、
   会话、甚至「有没有本地媒体」）—— **全都是无关的**，逐条对照见 §4。
3. **唯一决定成败的是「消息自己的时间」**：`DBInfo` 里那行 `Start Time` 是「本分片纪元」，
   `Sequence < StartTime` 的消息会被客户端当成**从别处导入的历史**，于是它**根本不查本地媒体**，
   直接去服务器下载 —— 而很多年前的 CDN 早就失效了。
4. 修法只有一个字段：把 `Start Time` 设成**早于你导入的最早一条消息**。
   实测：改完 105/105 条语音全部可播，而且客户端**跨会话不会把它改回去**。
   工具：`scripts/wxstart.py`。
5. 副作用：客户端会因此**滚出一个新分片** `MSG1.db` / `MediaMSG1.db` / `FTSMSG1.db`。
   这是 3.9 的正常多分片布局，但你的工具要按多分片写。`实测确认`。

---

## 1. 语音存在哪里

`Msg/Multi/MediaMSG0.db`

```sql
CREATE TABLE Media(Key TEXT PRIMARY KEY, Reserved0 INT, Buf BLOB,
                   Reserved1 INT, Reserved2 TEXT);
CREATE INDEX MediaReserved0Idx ON Media(Reserved0);
CREATE INDEX MediaReserved1Idx ON Media(Reserved1);
```

一行一条语音，字段含义（**实测确认**，来源是客户端自己写的那行）：

| 列 | 含义 |
|---|---|
| `Key` | 字符串形式的 `2^40 + MSG.localId` |
| `Reserved0` | `MSG.MsgSvrID`（服务端消息 id） |
| `Buf` | 音频字节。SILK 是 `\x02#!SILK_V3` 开头，AMR 是 `#!AMR` 开头 |
| `Reserved1` / `Reserved2` | 客户端写的是 `NULL` |

对应的 `MSG` 行是 `Type = 34`，`Reserved1 = 2`（普通消息是 1），
`StrContent` 是一段 `<msg><voicemsg …></msg>` XML，其中：
`voiceformat`、`voicelength`（毫秒，**同时决定气泡上显示的秒数**）、`length`（音频字节数）、
`bufid`、`aeskey`、`voiceurl`、`voicemd5`、`clientmsgid`、`fromusername`、`silklength`。

> **推断但很稳的一条**：`voiceformat="4"` 在本机既出现于 SILK 也出现于客户端自己录的语音，
> 所以**不要**拿它当编解码判据；判据要看 `Buf` 的魔数。

**权威样板的取法**（本文所有字段语义都来自这一招，见 §7）：
让客户端**自己**产生一条语音（用另一个账号发给它，或它自己发出去），
然后读它写进 `Media` / `MSG` 的那两行 —— 那是唯一不会骗人的规格说明书。

---

## 2. 一条不推荐的路：`Msg/Media.db.MediaInfo`

2.x 时代语音是存在 `Media.db` 的 `MediaInfo` 表里的（`MsgLocalId` 作主键，
音频字节塞在 `Thumbnail` 列）。我们一开始照着这个思路，把老库的 `MediaInfo` **整表搬进**
了 3.9 的 `Msg/Media.db`。

**这条路是错的**，`实测确认`：

* 3.9 **会创建** `Msg/Media.db` 的 `MediaInfo`（列：`MsgLocalId, TalkerId, MsgType,
  Reserved0, Reserved1, Thumbnail, Detail, Reserved2, Reserved3`），但**从不写它**；
  我们让客户端收语音、看快照，这张表始终 0 行。
* 我们搬进去的那批行里，有若干行的 `MsgLocalId` **指向根本不存在的消息**。
* 决定性对照：把这张表**整表清空**，语音照样能播（在修好 §5 之后）。

⇒ **不要写 `MediaInfo`。** 那属于「聊天记录 / 收藏」子系统，与正常播放无关。

---

## 3. 失败长什么样

气泡**能显示时长**（说明 XML 解析没问题），但：

* 点一下 → **红色感叹号**；或者**无限转圈**；
* 再点一下 → 弹「是否重新下载」。

这三个现象合起来指向同一件事：**客户端认为本地没有这条语音，于是走下载**。
`实测确认`（二进制里对应的符号是 `VoiceMgr::downloadVoice` / `doDownloadVoice` /
`onVoiceDownLoadFail`）。

---

## 4. 实测：**全都没关系**的东西

下面每一条都是一个**单变量对照**：拿一条**确定能播**的语音行复制若干份，
每份只改一个字段，然后逐条点。

| 改了什么 | 结果 | 说明 |
|---|---|---|
| 只换一个新的 id（纯副本） | ✅ 能播 | 复制这个动作本身不破坏任何东西 |
| `MsgSvrID` 换成一个很老的值 | ✅ 能播 | 服务端 id 不参与本地播放判定 |
| `MsgSequence` 换成一个很小的值 | ✅ 能播 | 每会话计数器不是闸门 |
| **把那行的本地 `Media` 行删掉** | ✅ 能播 | 客户端会去**服务器下载并且成功** ⇒ 下载通道没问题 |
| 把我们的 SILK 音频塞进客户端那行 | ✅ 能播 | 音频字节完全没问题，SILK 解码器工作正常 |
| 换到另一个会话 | ✅ 能播 | 会话（以及它的 `ChatInfo` 阅读锚点）不是闸门 |
| `BytesExtra` 里补/删 `msgsource`、调换字段顺序 | ✅ 能播 | 顺序与 `msgsource` 都无关 |
| **`CreateTime` / `Sequence` 换成很多年前** | ❌ **转圈** | **唯一会失败的变量** |

对照的干净程度值得强调：上表最后一行那个副本，**除了时间以外一个字节都没改**，
音频、`Media` 行、`BytesExtra`、会话全都是能播那一套 —— 它就是哑的。

> 顺带排除掉的还有一批「我们以为很像但无关」的字段：26 列逐列比对下来，
> 我们导入的行与客户端自己写的行之间，只有**身份列与时间列**天然不同。
> 也就是说：**行本身早就对了。**

---

## 5. 闸门：`DBInfo` 里的「本分片纪元」

`Msg/Multi/MSG0.db` 有一张两行的元数据表：

```sql
CREATE TABLE DBInfo (tableIndex INTEGER PRIMARY KEY,
                     tableVersion INT, tableDesc TEXT);
```

实测的初始内容（**这两行是客户端自己写的**，我们的脚本从未写过它）：

| tableIndex | tableVersion | tableDesc |
|---|---|---|
| 1 | `<TS_MS>`（该库创建时刻的毫秒时间戳） | `'Start Time'` |
| 2 | `1` | `'Prefix LocalId Index'` |

`Start Time` 这个字面量在 `WeChatWin.dll` 里以 **UTF-16** 形式存在（`实测确认`），
同一份二进制里还有这些符号（说明这条路径确实存在）：

```
Prefix LocalId Index < 0, Need Reset
Prefix LocalId Index No Need To Fix
PrefixLocalId Index NotFound : %d
msg with PrefixLocalId %d not found
MultiDBMsgMgr::ConvertToPrefixLocalId
ChatMgr::GetMgrByPrefixLocalId
MultiDBMsgMgr::FixPrefixLocalIdIndex
```

**机制（`推断`，但与 §4 的八条对照全部吻合）**

```
Sequence >= DBInfo.'Start Time'  ->  先查本地媒体 MediaMSG0.Media，命中即播
Sequence <  DBInfo.'Start Time'  ->  当作「从别处导入的历史」，跳过本地媒体，直接去服务器下载
```

我们导入的历史消息全部落在该纪元**之前**，所以每一条都被绕开了本地音频 ——
而它们的 `Media` 行一直都在那里，一直是对的。

**修法**（`scripts/wxstart.py`）

```bash
python3 scripts/wxstart.py check  --db <MSG0.db> --key-env WX_AES_KEY
python3 scripts/wxstart.py build  --db <MSG0.db> --key-env WX_AES_KEY --auto
python3 scripts/wxstart.py verify --db <MSG0.db> --key-env WX_AES_KEY
python3 scripts/wxstart.py install --db <MSG0.db> --key-env WX_AES_KEY
```

`--auto` 会把 `Start Time` 设成**早于最早一条消息**的值（我们示例用 `1400000000000`）。
改完重启客户端，语音即可播放。**`实测确认`：105/105 条全部可播。**

**副作用**：客户端会把当前分片视为「太老」，于是新建
`MSG1.db` / `MediaMSG1.db` / `FTSMSG1.db`（`实测确认`）——
即 `Start Time` 同时也承担「本分片纪元」的角色。这**不影响**旧数据与语音的显示，
但你的工具必须遍历**所有** `MSG*.db` / `MediaMSG*.db`，备份与 `-wal` 检查也要覆盖全部。

---

## 6. 导入时应该怎么排顺序

1. 先按 `row-mapping.md` 把行写进 `MSG0.db`（`Sequence = CreateTime*1000` 这一步别写错）。
2. 再按 `media-layout.md` 把媒体文件放到路径上。
3. 再写 `MediaMSG0.db.Media` 的 `Media` 行（`media-layout.md` / `wxvoice.py`）。
4. **最后**设 `DBInfo.'Start Time'`（`wxstart.py --auto`）。
   放在最后是因为它取决于「最早一条消息的 `Sequence`」。
5. 干净退出客户端再做替换（见 `../appendix/pitfalls.md` 的三道闸）。

---

## 7. 方法：**从「已知能播的那一行」做减法**

这次能找到闸门，靠的不是读二进制，而是这套实验设计。它可以直接复用到别的
「数据明明在、客户端就是不认」的问题上：

1. **先找到一条能播的**。让客户端**自己**产生一条同类消息（换个账号发给它 / 让它自己发一条）。
   这条行就是「正确形状」的唯一权威来源。
2. **复制它，一份只改一个字段**。不要一次改一堆 —— 那样失败了也不知道是谁的锅。
3. **让它自己省人力：用显示时长当编号**。把每份副本的 `voicelength` 改成互不相同的值
   （例如 3″/4″/5″…），用户就能按气泡上的秒数逐条点、逐条报，不需要理解任何技术细节。
4. **配一个「快照 → 差分」工具**（`scripts/wxsync.py`）：让客户端做完一次操作，
   对比操作前后**整个账号数据根**的文件大小 / mtime / 表行数 / 内容哈希。
   这样能直接看到**客户端自己写了什么**，而不是猜。
5. **注意「无关变量」也要各自成一条对照**。我们正是因为把「会不会是会话的问题」
   「会不会是本地媒体缺失的问题」也做成了独立副本，才敢断定闸门只有一个。

**顺带查明的一件事**（也是 §4 那条方法得到的）：客户端**成功播放**一条语音时，
只会往那一行的 `BytesExtra` 里补一个 **`子类型 5 = 1`**（外层字段1，`0a 04 08 05 10 01`）——
一个「播过了」的标记。除此之外它不写任何媒体状态，`Media` 表一行都不动。
反过来说：**播放失败时它也不会记下"失败"**，所以不要指望靠库里的状态反推"哪些播过"。

---

## 8. 边界与未验证的部分

* 本文参数在 **3.9.12.56 (x86)** 上实测。其它版本请自行验证；`DBInfo` 的语义
  我们没有在别的版本上确认过。
* `Start Time` 的**确切比较规则**（是 `<` 还是 `<=`、是否还参与别的判定）属于 `推断`：
  我们验证的是「设得足够早 ⇒ 能播」这个方向，没有穷举边界值。
* 「把 `Start Time` 设早会让客户端滚新分片」是 `实测确认`，但**它是否在所有版本上都会发生**未验证。
* `MediaInfo` 那张表**是否在别的场景下会被用到**（例如聊天记录导出）我们**没有验证**，
  只能说「写它对本机正常播放没有帮助，且可能有害」。
* 本文不涉及 4.x：语音在 4.x 里如何落位属于 [`4x-migration.md`](4x-migration.md) 的范畴。
