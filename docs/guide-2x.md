# 第 1 步：救出十年前那批老客户端数据

> 给**手上还有一份十年前的微信 2.x 数据目录**的人：让 `WeChat 2.0.0.37` 重新登录、
> 把旧库打开，在动手之前先把数据整盘固化下来，最后导出成可读文本 / CSV。
> 全程只处理**你自己的账号、你自己的设备、你自己的数据**。

---

## 目标

1. 让 `WeChat 2.0.0.37`（十年前编译的版本）**能正常登录**，并且**登录后不掉线**；
2. 让客户端**认到**那个十年前的数据目录，把 `Msg\*.db` 真正打开；
3. 在任何写操作之前，先把**程序目录 + 数据根**整盘复制归档一份；
4. 把已经解开（明文）的库导出成**可读聊天记录 / CSV / 分会话 txt**。

做完你会得到：一份**未被动过**的原始归档、一份可反复使用的 2.x 数据副本、
以及可直接阅读/检索的导出结果。想继续往现代客户端迁移，见 [`guide-3x.md`](guide-3x.md)。

---

## 前提

* **客户端**：`WeChat 2.0.0.37`（x86，十年前编译），放在**独立目录**里，不要和现代版混住。
  校验 `WeChat.exe`：大小 **7,576,272** 字节、文件版本 **2.0.0.37**。
* **系统**：Windows 10 x64。第 2–5 步要 Windows 侧进程，纯 Linux 环境跑不了。
* **数据**：一份十年前的账号目录，`Msg\` 里是 2.x 布局
  （`ChatMsg.db` / `MicroMsg.db` / `Misc.db` / `Media.db` / `Favorite.db` /
  `BizChat.db` / `BizChatMsg.db` / `Emotion.db`）。
  **没有** `Multi\MSG0.db` 才是 2.x —— 有 `Multi\` 说明是 3.x 布局。
* **明文库**（第 6 步用）：一份已经解开、可被 `sqlite3` 直接打开的 `*.db` 副本。
  **它来自你自己**：你先得是账号主人，并在你自己的设备上以正常方式登录（扫码 / 密码）；
  本仓库不涉及破解加密、不涉及撞库或猜测口令、不涉及绕过认证。**它的取得步骤不在本文里。**
* **磁盘**：预留**至少原数据根 3 倍**空间（原始归档 + 工作副本 + 导出结果）。

> ⚠️ **一次性机会的警告（先读完再扫码）**
> 服务端对 **(账号, 设备)** 这一对的「新设备豁免」**只能成功一次**。
> 也就是说：**同一台机器、同一个客户端、同一个账号，你大概率只有一次扫码成功的机会。**
> 机会用掉之后，再扫就是
> `[MMPC_NetSceneAuth] NetSceneAuth log fail mRetCode = -800000` / `auth err uin: 0`，
> 和「机器被拉黑」不是一回事 —— 换账号扫会正常弹「新设备，等待 5 秒确认」。
> **想重来，需要的是在固件层就是另一台机器的设备（虚拟机天然满足）。**
> ⇒ 第 0 步（全部一次性准备）**必须在扫码之前做完**，顺序不要换。

---

## 步骤

### 第 0 步：扫码之前，把这几件事全部做完

| # | 动作 | 不做会怎样 |
|---|---|---|
| 1 | 升级器已改名停用（第 2 步） | 登录后程序文件被静默替换，这次机会等于白扫 |
| 2 | 登出标记已清空、看门狗已就绪（第 3 步） | 客户端自己把自己踢下线，窗口只剩约 11 分钟 |
| 3 | 旧数据已放进客户端**真正会读**的数据根（第 4 步） | 客户端建一套空库，机会用掉了却没有数据 |
| 4 | 备份已经完成并核对过（第 1 步） | 出问题时没有可回退的原始归档 |

第 3 条最容易翻车：**数据根路径会多拼一层 `WeChat Files`**，见第 4 步。

---

### 第 1 步：先固化，再动手（整盘归档）

**做什么**：把**程序目录**和**数据根**各做一份完整副本，落在**独立的 Linux/ext4 卷**
或至少与工作盘不同的物理盘上。这一步是第 2 步之后所有操作的回退点。

```bat
robocopy "<WECHAT_DIR>"      "<ARCHIVE_DIR>\wechat-2.0.0.37-dir" /E /COPY:DAT /R:1 /W:1
robocopy "<WECHAT_FILES_ROOT>" "<ARCHIVE_DIR>\wechat-files"     /E /COPY:DAT /R:1 /W:1
```

**预期结果 / 验收**：两份副本的文件数、总字节数与源一致（`robocopy` 结尾汇总表里
`失败 = 0`）。

> 保真归档请落在 **ext4** 卷上：NTFS/drvfs 会把越界的时间戳夹到端点
> （你会看到「2446 年」的文件，那不是硬盘坏了，是文件系统在替非法值兜底）。

---

### 第 2 步：停用自带升级器（漏了这步前功尽弃）

**做什么**：`WeChat 2.0.0.37` 目录里有 `WechatUpdate.exe`，**登录后会自动跑**，
静默把程序文件替换成当前最新版。先把它改名。

```bat
cd /d "<WECHAT_DIR>"
ren WechatUpdate.exe      WechatUpdate.exe.disabled
ren WechatUpdate.exe.tmp1 WechatUpdate.exe.tmp1.disabled
```

**预期结果 / 验收**：目录里不再出现 `WechatUpdate.exe` / `WeChatUpdate.exe` /
`WeChatUpdate.bin`（大小写两种写法都查一遍）。

> 想恢复自动升级能力，把上面两条 `ren` 反过来即可。

---

### 第 3 步：处理「自我登出」并启动客户端

**做什么**：停用升级器**还不够**。服务器会下发最低版本要求，客户端读到就
**自己把自己登出**，并把标记落盘；此后每次启动都会拒绝登录。
启动脚本在启动前清掉这两个标记，并带上兼容层：

```bat
@echo off
set "__COMPAT_LAYER=~ ARM64WOWONAMD64"
del /f /q "%APPDATA%\Tencent\WeChat\All Users\config\update.data"
del /f /q "%APPDATA%\Tencent\WeChat\All Users\config\25d2ec66.ini"
cd /d "<WECHAT_DIR>"
start "" "WeChat.exe"
```

**预期结果 / 验收**：客户端正常启动，**不弹「版本过低」**，且跑的还是 2.0.0.37：

```powershell
Get-Process WeChat | Select-Object Id, Path, @{n='FV';e={$_.MainModule.FileVersionInfo.FileVersion}}
# Path : <WECHAT_DIR>\WeChat.exe
# FV   : 2.0.0.37
```

> 那个复查周期是 **660 秒（约 11 分钟）** 一次，所以「只有第一次能登录」的真相是
> **每次登录后有且仅有第一个约 11 分钟的窗口能用**。想让登录后长时间待机，
> 就再加一个看门狗：运行期间每隔 **300 ms** 删一次上面那两个文件
> （客户端写入到登出之间有约 5 秒，足够命中）。

---

### 第 4 步：把旧数据放进客户端**真正会读**的数据根

**做什么**：微信拼路径的规则是 `账号数据根 = HKCU\Software\Tencent\WeChat\FileSavePath
+ "\WeChat Files"`，账号目录 = `<账号数据根>\<账号名>\`。**注意会再拼一层 `WeChat Files`**：

| 注册表 `FileSavePath` | 实际使用的数据根 |
|---|---|
| `C:\Users\<user>\Documents` | `C:\Users\<user>\Documents\WeChat Files` |
| `C:\Users\<user>\Documents\WeChat Files` | `…\Documents\WeChat Files\WeChat Files` ← 双层 |

把十年前的账号目录（**至少**含 `Msg\`、`config\`，最好连
`Data\ Image\ Video\ Attachment\ CustomEmotions\` 一起）放进**按上表推算出来的那个**
账号目录；先看清客户端会去哪个目录找：

```powershell
Get-ItemProperty 'HKCU:\Software\Tencent\WeChat' -Name FileSavePath
```

**预期结果 / 验收**：`Msg\` 下确实是 2.x 布局的 8 个库，且路径按上表推算正确
（**少拼或多拼一层 `WeChat Files` 是最常见的失败原因**）。

---

### 第 5 步：扫码登录（唯一一次，扫码前再核对一遍第 0 步）

**做什么**：手机上正常扫码。因为升级器已停用、标记已清空，**不会再被替换**，
也不需要「第一次」这种运气；但**机会本身仍然是一次性的**。

推荐时序：① 确认第 0 步已完成 → ② 手机扫码 → ③ 手机出现「新设备，等待 5 秒确认」
（窗口开始，此时本地库已被客户端独占打开）→ ④ 界面出现聊天列表、能打开那批老记录。

**预期结果 / 验收**（三条全中，才算这一次没有白费）：

| # | 判据 | 怎么查 |
|---|---|---|
| 1 | `WeChat.exe` 文件版本**没有被替换** | 第 3 步那行 PowerShell |
| 2 | 目标 `.db` 被客户端**独占打开**、mtime 变了 | 试着用别的程序复制该文件；或看 `update_*.log` 里有没有 `silent update` |
| 3 | 能读到那批老数据里的会话与消息 | 在客户端里翻聊天记录 |

只有 1、2 中而 3 不中，说明客户端打开的不是你以为的那个库 —— 回第 4 步
（**数据根多拼/少拼一层 `WeChat Files` 是最常见原因**）。

---

### 第 6 步：把明文库导出成可读文本 / CSV

**做什么**：对**已经解开的明文 `*.db` 副本**运行导出脚本。
该脚本**只读**明文库、只写输出目录，不碰客户端、不碰原始归档。

```bash
python3 scripts/export_2x.py --plain /path/plain --out /path/recovered --self <账号名>
```

（也认环境变量 `WX_PLAIN_DIR` / `WX_OUT_DIR` / `WX_SELF`；只给参数或只给环境变量都行。
先 `python3 scripts/export_2x.py --help` 看全部选项。）

**预期结果 / 验收**：`--out` 目录下出现 `README.md`、`聊天记录_全部.txt`、
`聊天记录_按会话/<序号>_<名称>.txt`、`消息.csv`、`联系人.csv`、
`00_解密数据库/*.db`；消息条数与你手上副本一致：

```bash
sqlite3 /path/plain/ChatMsg.db 'pragma integrity_check; select count(*) from ChatMsg;'
```

> 导出结果里含**真实会话名与真实消息内容**，请当作**个人数据**保存，
> 不要提交进任何公开仓库。

---

## 对应脚本

| 脚本 | 作用 | 关键参数 |
|---|---|---|
| `scripts/export_2x.py` | 明文库 → 可读聊天日志 / CSV / 分会话 txt | `--plain`（默认 `WeChat_2x_plain`）、`--out`（默认 `WeChat_2x_recovered`）、`--self`（默认 `MyAccount`）；同名环境变量 `WX_PLAIN_DIR` / `WX_OUT_DIR` / `WX_SELF` 作为兜底。`--selftest` 用虚构数据端到端跑一遍 |
| `scripts/selftest.sh` | 冒烟自检：所有脚本 `--help` 可用、`--selftest` 通过、shell 语法 | `bash scripts/selftest.sh` |
| `scripts/check_no_secrets.sh` | 发布前自检：个人路径 / 账号标识 / 危险文件类型等 | `bash scripts/check_no_secrets.sh .`（工作区）、`--history`（全部提交） |

> 登录、备份、看门狗这几步目前是**命令行 + 注册表**操作，没有独立脚本入口，
> 照抄本文命令即可。

---

## 常见失败

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 第一次能登录，之后每次都「版本过低」，且**程序文件看着没变** | 自带 `WechatUpdate.exe` 已把程序文件静默替换（只换 30 多个文件，肉眼看不出来） | 回第 2 步改名停用；用 **`WeChat.exe` 的大小 + 文件版本**判断，别看文件夹 |
| 登录成功，**约 11 分钟后**被踢下线，此后每次扫码都 `retCode = -800000` | 服务器下发最低版本要求，客户端读到就写盘标记并自我登出 | 重启前清 `update.data` / `25d2ec66.ini`；或上 300 ms 看门狗（第 3 步） |
| 扫码后界面是空的 / 建的是一套**空库** | 数据根 != 客户端实际使用的目录（通常少拼或多拼了一层 `WeChat Files`） | 按第 4 步的表核对注册表 `FileSavePath`，把账号目录挪到推算出的位置 |
| 扫码提示失败，**换另一个账号却能正常弹确认** | **(账号, 设备)** 的新设备豁免已经用掉了 | 已经用掉就无法在原设备上重来 —— 需要一台在固件层就是另一台机器的设备（虚拟机天然满足） |
| 导出脚本报 `no such table` / 打不开库 | `WX_PLAIN_DIR` 里放的是**加密库**，不是明文副本 | 只对明文副本运行；明文副本的取得方式见 `## 前提` |

---

细节与证据：[`detail/launch-and-version-gate.md`](detail/launch-and-version-gate.md)、
[`appendix/pitfalls.md`](appendix/pitfalls.md)；拿到 2.x 数据之后 →
[`guide-3x.md`](guide-3x.md)。
