# 让微信 2.0.0.37 正常登录（彻底绕过「版本过低」）

> 实测环境：WeChat **2.0.0.37**（十年前编译，x86）/ Windows 10 x64
> 结果：**一次扫码登录成功**，十年前的 `Msg\*.db` 被客户端正常打开。

---

## 1. 先说结论：这不是服务器在拦你

网上关于「版本过低」的说法（服务器强制升级、封禁旧协议、设备指纹、注册表写标记）在
**微信 PC 2.x** 这个场景下都不是主因。真正的机制是两条叠加：

### 原因 A：客户端自带的升级器把自己换掉了（**决定性**）

2.0.0.37 程序目录里有一个 `WechatUpdate.exe`。**它在登录后会自动跑**，静默把程序文件
替换成当前最新版（实测换成 3.9.12.44，替换了 36 个文件）。

证据在明文日志 `%APPDATA%\Tencent\WeChat\log\update_YYYYMMDD.log`：

```
(<T1>) -v/workHandle:9a42ab38....zip,200,1661537324,4
(<T2>) -i/update:silent update
(<T3>) -i/UpdateMgr:14 file rename success
(<T4>) -i/UpdateMgr:update success, replaced file count: 36
```

`1661537324 = 0x63090C2C` = **3.9.12.44**。（版本号编码 = `0x6` + 主版本 + 两位次版本 + 两位修订 + 两位构建，例如
`3.9.12.56 → 0x63090c38`。）

**所以「第一次能登录、之后就不行」是个假象**：第一次你跑的确实是 2.0.0.37；
从第二次起，你跑的东西已经不是它了。删用户文件夹、改程序目录名都无效，因为变量根本不在那儿。

### 原因 B：32 位客户端跑在 64 位系统上会误报

微信 3.9.12 的 **x86** 版在 **64 位** Windows 上会被判定为「版本过低」——这是平台探测
产生的误报，与服务器无关。社区两个独立项目的解法完全一致：

* [`Skyler1n/WeChat3.9-32bit-Compatibility-Launcher`](https://github.com/Skyler1n/WeChat3.9-32bit-Compatibility-Launcher)
  → `set "__COMPAT_LAYER=~ ARM64WOWONAMD64"`
* [`ThinkerWen/FakeWechatVersion`](https://github.com/ThinkerWen/FakeWechatVersion)
  → 不给 `c=`/`t=` 参数时，它**唯一做的事**就是带 `__COMPAT_LAYER=~ ARM64WOWONAMD64` 启动微信

> 如果你被升级器换成了 3.9.12.x，那么拦住你的是原因 B。停用升级器后就不会走到这一步。

### 原因 A 的旁证：程序目录**具体**被加了什么（**实测确认**）

我们手上有两份 2.0.0.37 程序目录：一份**从未运行过**（干净基准），一份**跑过一次登录**。
对两棵树做全量 `路径 + 大小` 比对：

| 项 | 结果 |
|---|---|
| 只在"跑过"的那份里存在 | **21 个文件，全部位于程序目录** |
| 只在基准里存在（丢失） | **0** |
| 共有文件的大小不一致 | **0** |
| 关键文件 MD5（`WeChat.exe`、`WeChatWin.dll` + 8 个库） | **10 / 10 完全一致** |
| 21 个里带"运行当日"mtime 的 | **8 个** |

21 个模块名（都是微信自己的组件，不含任何个人信息）：

```
andromeda.dll   cldnn_ns_16k.bin   ConfSdk.dll      ilink2.dll       libwxcodec.dll
mmcrashpad_client32.dll            mmcrashpad_handler32.exe         mmmojo.dll
mmmojo_64.dll   mmtcmalloc.dll     owl.dll          pagengine.dll    plugin_info.ini
ThumbPlayer.bin WeChatUpdate.bin   WeChatUtility.bin                 WechatCodec.exe
WeUIResource.dll WetypeInstaller.exe
```

① 20 个 `.dll`/`.exe`/`.bin` 组件 + `plugin_info.ini`，合计 21 个。

**结论**：跑一次旧客户端的副作用是**往程序目录里加模块**，而不是"什么都不改"。
（**推断**：由运行中的客户端 / 升级器写入 —— 依据是其中 8 个带运行当日 mtime，
且模块名全是升级器与运行时组件。）

这也是 §2 第 0 步要求"先留一份干净的程序目录"的原因：
**你手上那份很可能已经不干净了，而它不会告诉你。**

> **一个顺带挖出来的通用现象（`实测确认` + `推断`）**：这些新加进来的模块带着**越界的时间戳**。
> 当它们被写进 Linux 卷（例如 NAS 上的 ext4）时，内核会把非法值**夹到 ext4 可表示区间的两个端点**：
> `1901-12-13 20:45:52` 与 `2446-05-10 22:38:55`。
> 实测：这两个值恰好是 ext4 的最小/最大时间戳，而承载它们的那块卷确实是 ext4；
> 同一批文件在 ext4 与 NTFS 之间搬动时，负值还会在 NTFS 侧被夹成 `0`（1970-01-01）。
> 所以当你在文件管理器里看到 **"2446 年"** 的文件时，那**不是硬盘坏了**——
> 是**文件系统在替一个非法时间戳兜底**。
> （推论：**任何"忠实备份"都必须先确认落地文件系统的时间戳值域**，否则你以为存下来了，其实已经被改过。）

---

## 2. 操作步骤

### 第 0 步：准备一份干净的 2.0.0.37 程序目录

校验 `WeChat.exe`：大小 **7,576,272** 字节，文件版本 **2.0.0.37**（编译日期以文件属性为准）。

### 第 1 步：停用升级器（**最关键，漏了这步前功尽弃**）

```bat
cd /d "<WECHAT_DIR>"
ren WechatUpdate.exe      WechatUpdate.exe.disabled
ren WechatUpdate.exe.tmp1 WechatUpdate.exe.tmp1.disabled
```

检查目录里不再出现 `WechatUpdate.exe` / `WeChatUpdate.exe` / `WeChatUpdate.bin`。

> 想恢复（让它可以再自动升级）就把上面两条 `ren` 反过来。

### 第 2 步：用兼容层启动

`1-启动-2.0.0.37.cmd`：

```bat
@echo off
set "__COMPAT_LAYER=~ ARM64WOWONAMD64"
cd /d "<WECHAT_DIR>"
start "" "WeChat.exe"
```

或者写进注册表永久生效（注意路径里的反斜杠要转义成 `\\`）：

```reg
Windows Registry Editor Version 5.00

[HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\AppCompatFlags\Layers]
"<WECHAT_DIR>\\WeChat.exe"="~ ARM64WOWONAMD64"
```

### 第 3 步：让客户端找到旧数据（**最容易搞错的一步**）

微信的路径规则是：

```
账号数据根 =  HKCU\Software\Tencent\WeChat\FileSavePath  +  "\WeChat Files"
账号目录   =  <账号数据根>\<账号名>\
```

**注意会再拼一层 `WeChat Files`。** 实测：

| `FileSavePath` | 实际使用的数据根 |
|---|---|
| `C:\Users\<user>\Documents` | `C:\Users\<user>\Documents\WeChat Files` |
| `C:\Users\<user>\Documents\WeChat Files` | `C:\Users\<user>\Documents\WeChat Files\WeChat Files` ← 双层 |

所以把十年前的 `MyAccount` 目录（至少含 `Msg\`、`config\`，最好连
`Data\ Image\ Video\ Attachment\ CustomEmotions\` 一起）放进**推算出来的那个账号目录**。

`Msg\` 里应该是 2.x 布局：

```
BizChat.db  BizChatMsg.db  ChatMsg.db  Emotion.db  Favorite.db  Media.db  MicroMsg.db  Misc.db
```

（**没有** `Multi\MSG0.db` 才是 2.x；3.x 会把它搬进 `Msg\<uin>\` 并另建 `Multi\`。）

### 第 4 步：扫码登录

正常扫码即可。因为升级器已经停用，**不会再被替换**，也不需要「第一次」这种运气。

---

## 3. 怎么确认这次真的成功

```powershell
Get-Process WeChat | Select-Object Id, Path, @{n='FV';e={$_.MainModule.FileVersionInfo.FileVersion}}
```

应该输出：

```
Path : <WECHAT_DIR>\WeChat.exe
FV   : 2.0.0.37
```

再确认升级器没有偷偷活动过：

```powershell
Get-Content "$env:APPDATA\Tencent\WeChat\log\update_$(Get-Date -f yyyyMMdd).log"
```

**不应**再出现 `silent update` / `update success`。

---

## 4. 踩坑清单

| 现象 / 做法 | 实际结果 |
|---|---|
| 删掉 `WeChat Files` 用户文件夹再登 | **无效**，变量不在那里 |
| 把程序目录改名 | **无效**，同上 |
| 改 exe 的版本资源、内存改版本号、hosts 屏蔽、中间人代理 | 对 **2.0.0.37** 都没必要——它本来就能登，问题不在这里 |
| 只看了「文件夹好像没变化」 | 升级器只替换 36 个文件，夹杂在不同年份的 DLL 里，肉眼看不出来。**要看 `WeChat.exe` 的大小和文件版本** |
| 升级器 `.tmp1` 留着 | 无害，但一起改名更保险 |
| 以为 `HKCU\Software\Tencent\WeChat\Version` 是拦路虎 | 它只是记录**上次安装/升级到的版本**（`0x63090c38` = 3.9.12.56），不是闸门 |

---

## 5. 一个可复用的判断法

拿到一个「打不开」的微信程序目录，先做三件事：

1. `WeChat.exe` 的**文件版本**是多少？和目录里 `WeChatWin.dll` / `WeChatResource.dll`
   的版本是否一致？（不一致 = 被升级过，是缝合怪）
2. 目录里有没有 `[3.9.12.56]` 这类**方括号版本目录**？（WeChat 3.x 的更新暂存目录）
3. `%APPDATA%\Tencent\WeChat\log\update_*.log` 里有没有 `silent update`？

三条里有任意一条命中，说明你面对的已经不是你以为的那个版本了。

---

## 6. ⚠️ 补充：**停用升级器还不够** —— 客户端会自己把自己踢下线

这是实测中踩到的第二个坑，比第一个更隐蔽。

### 现象

停用升级器后第一次登录**完全成功**（能看那批老记录、能同步），但**大约 11 分钟后**
客户端自动掉线；此后每次启动扫码都提示「版本过低」，且**程序文件没有被替换**
（`WeChat.exe` 仍是 2.0.0.37）。

### 原因

明文错误日志 `%APPDATA%\Tencent\WeChat\log\MM_YYYYMMDD.err`：

```
11:56:09        （客户端写入 All Users\config\update.data 和 25d2ec66.ini）
11:56:14.391   [MMPC_NetSceneAuth]  NetSceneAuth log fail mRetCode = -800000
11:56:14.391   [MMPC_NetSceneStatusNotify] mRetCode: -8888
11:56:14.391   [MMPC_NetSceneLogOut] mRetCode: -800000
11:56:14.978   [MMPC_UpdateMgr] launchUpdateExe: openExe err,cmd:9a42ab38...zip,200,1661537324,...
```

`update.data` 是**明文 protobuf**，里面直接写着服务器下发的最低版本：

```
<extInfo silence="1" safeurl="1" MinVersion="0x63090C00">
url=http://dldir1.qq.com/weixin/Windows/WeChat_3.9.12_update44.zip?toclientver=1661537324&from=getupdateinfo
```

`0x63090C00` = **3.9.12.0**。2.0.0.37 远低于它，客户端于是：

1. 把更新信息**落盘**到 `All Users\config\update.data`（+ 一个内容为 `"1"` 的 `25d2ec66.ini`）
2. **自己把自己登出**（`NetSceneLogOut`）
3. 尝试拉起升级器（我们把它改名了，所以 `openExe err`）
4. 之后每次启动读到这个文件 → **拒绝登录**

### 为什么恰好是 11 分钟

`%APPDATA%\Tencent\WeChat\1\host\query_interval.ini`：

```ini
[8eecfe097a1fcda5aa2413ee84fc9d08]
lastquerytime=10298093
name=queryinterval
queryintervalsecond=660      ← 每 660 秒（11 分钟）向服务器复查一次
```

所以「**只有第一次能登录**」的真相是：**每次登录后有且仅有第一个 11 分钟窗口能用。**

### 处理办法

**A. 每次启动前清掉触发文件**（简单可靠）：

```bat
del /f /q "%APPDATA%\Tencent\WeChat\All Users\config\update.data"
del /f /q "%APPDATA%\Tencent\WeChat\All Users\config\25d2ec66.ini"
```

这样每次启动都能登录，能用一个 11 分钟窗口。

**B. 加一个看门狗**，在微信运行期间每 300 ms 删一次这两个文件
（客户端写入到登出之间有 5 秒，300 ms 的循环有足够机会命中），
顺便把 `queryintervalsecond` 改大：

见 `<LAUNCHER_DIR>\1-启动-2.0.0.37.cmd` + `wx2x_watchdog.ps1`。

**C. 想要彻底根治**，得让客户端拿不到 `getupdateinfo` 的回复。
但这条走的是微信自己的长连接（不是 HTTP CGI），
`cgi-mapping_*.xml` / `host-redirect.xml` 里找不到对应主机，
所以**改 hosts 是无效的** —— 只能靠 A/B，或者对客户端做内存/二进制补丁。

### 一句话总结

| 层次 | 机制 | 对策 |
|---|---|---|
| 1 | 自带 `WechatUpdate.exe` 静默替换程序文件 | 改名停用 |
| 2 | 服务器下发 `MinVersion=3.9.12.0` | —— |
| 3 | 客户端每 660 s 复查，拿到就**自我登出**并落盘标记 | 清标记 + 看门狗 |

**只做第 1 条，你会得到一个「每次登录只能用 11 分钟」的微信。**

---

## 7. 这一次可能只有一次：扫码前的准备清单

**为什么当一次性资源对待**：服务器对 `(账号, 设备)` 的新设备豁免**只成功一次**。
第一次扫码之后，这台机器 + 这个客户端版本再扫同一个账号，会一直返回同一个失败码。
想重来必须换一台**真正不同**的设备（虚拟机也行）。

所以在扫码之前，把下面几件事做完：

| # | 准备项 | 漏了会怎样 |
|---|---|---|
| 1 | 升级器已改名停用（§2 第 1 步） | 登录后程序文件被静默替换，这次机会白费 |
| 2 | `MinVersion` 标记已清空、看门狗已就绪（§6） | 约 11 分钟后被客户端自己踢下线 |
| 3 | 旧数据已放进客户端**真正会读**的数据根（§2 第 3 步 —— 注意会再拼一层 `WeChat Files`） | 客户端建一套空库，你以为成功了，其实什么都没读到 |
| 4 | 想清楚这次要处理**哪个账号 / 哪一代**的数据根 | 目标搞错，还得再等一次机会 |

**成功判据**（只与"账号是否被这台设备接受"有关）：

| # | 判据 | 怎么查 |
|---|---|---|
| 1 | 程序目录里的 `WeChat.exe` **文件版本没有被替换** | §3 的那行 PowerShell |
| 2 | 目标 `.db` 被**独占锁定**、或 mtime 变了 | 试着复制该文件；或看 `update_*.log` 里有没有 `silent update` |

> 想零成本地预览"成功路径上的界面长什么样"，可以先用**另一个**账号扫一次 ——
> 那个账号本来就会正常弹「新设备，等待 5 秒确认」，**不会**消耗原账号的机会。

**这是一次性的。** 把上表准备好再扫码。
