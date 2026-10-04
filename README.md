# wx-archive-migrate

> 把十年前的微信本地聊天归档救出来，并**逐级**搬进现代客户端：
> **2.0.0.37 → 3.9.12.56 → 4.x**。
> 面向的是**你自己的账号、你自己的设备、你自己的数据**。

---

## 这是什么

老记录的「打不开」其实是**三个独立问题**叠在一起，各有各的最小解法：

| 你看到的现象 | 真正的原因 | 解法 |
|---|---|---|
| 老客户端提示「版本过低」，登录就被踢下线 | 它自带的升级器会**静默把自己替换成最新版**；服务端下发的 `MinVersion` 会被**持久化后自我登出**；新设备豁免**只能成功一次** | 改名停用升级器、清掉标记、必要时换一台真正不同的设备 |
| `Msg\*.db` 打不开 | 库是 SQLCipher 加密的 | 用**你自己已有的密钥** + 本仓库的加解密工具 |
| 迁进新客户端后，图片/语音/文件不显示 | 客户端按**记录下来的路径**找媒体（不按内容哈希）；语音还卡在一个隐藏的**时间闸门**上 | 把文件放到客户端期望的位置与形状 |

**与同类项目的差异**：别人通常只覆盖一个版本段；这里是一条**端到端**的链，而且
**3.9 → 4.1 这一段走的是官方功能**（不是自己拼库），所以能被客户端正常接受。

---

## 三步走

| # | 这一步要解决什么 | 指南 | 完成标志 |
|---|---|---|---|
| **1** | 让旧版客户端**能登录**，并把数据根固化下来 | [`docs/guide-2x.md`](docs/guide-2x.md) | 客户端能显示会话；数据根已整盘备份 |
| **2** | 把 2.x 的历史做成 **3.9 能读的形状** | [`docs/guide-3x.md`](docs/guide-3x.md) | 客户端里能看到那批历史的文本 / 图片 / 视频 / 表情 / 文件 / 语音 |
| **3** | 用官方「**加载历史聊天记录**」迁进 **4.x** | [`docs/guide-4x.md`](docs/guide-4x.md) | 4.x 里逐月对得上，媒体能正常打开 |
| **附加** | 附件重复 / 孤立审计 + **硬链接折叠** | [`docs/guide-attachments.md`](docs/guide-attachments.md) | 路径条目一个不少，占用下降 |

> **先看附录再动手**：[`docs/appendix/pitfalls.md`](docs/appendix/pitfalls.md)（写库前后的坑，症状 → 原因 → 做法）、
> [`docs/appendix/dead-ends.md`](docs/appendix/dead-ends.md)（**已经堵死的路**，含总表）。
> 这两篇能省掉你反复试错的时间。

---

## 快速开始

```bash
git clone <this-repo> && cd wx-archive-migrate
pip install -r requirements.txt            # 只有 cryptography 一条；不装也能看 --help

# 环境：Windows（本项目的实测平台）+ Python 3.12；Linux/WSL 可用于只读分析
bash scripts/selftest.sh                   # 冒烟自检：每个脚本的 --help 与 --selftest

python3 scripts/dump_mem.py    --help      # 2.x：转储 32 位进程内存 → 扁平文件 + 索引
python3 scripts/wxdec.py       --help      # 2.x：用你自己的密钥解密单个库
python3 scripts/export_2x.py --help      # 2.x：明文库 → 聊天日志 / CSV / 分会话 txt

python3 scripts/wxdec3x.py --help          # 3.x 加解密
python3 scripts/wxrows.py  --help          # 2.x → 3.x 行迁移
python3 scripts/wxmedia.py --help          # 媒体落位
python3 scripts/wxvoice.py --help          # 语音
python3 scripts/wxstart.py --help          # DBInfo.'Start Time'（语音时间闸门）
```

4.x 侧（Windows 侧 Python 运行；WSL 里请用 `python.exe -X utf8`）：

```bash
python3 scripts/wx4_decrypt.py --help          # 用你自己的密钥解密 4.x 库
python3 scripts/wx4_attach_inventory.py --help # 附件清单 + 双哈希
python3 scripts/wx4_attach_refs.py --help      # 从库里抽引用集
python3 scripts/wx4_attach_audit.py --help     # 比对出「重复 / 孤立」报告
python3 scripts/wx4_attach_dedup.py --help     # 硬链接折叠（默认只出计划）
```

每个脚本都是**先报告、后动手**：`check` → `build` → `verify` → `install`（或 `plan` → `apply`），
默认动作永远是只读。细节见 [`docs/README.md`](docs/README.md)。

---

## 仓库布局

| 位置 | 内容 |
|---|---|
| `docs/guide-*.md` | **怎么一步步做**（面向使用者，命令级） |
| `docs/detail/` | 展开的细节与原理（格式、参数、布局、为什么这样做） |
| `docs/appendix/` | **不是使用说明**：踩坑清单、已证伪结论、证据索引、下架处理手册 |
| `scripts/` | 工具。每个脚本都支持 `--help`，带 `--selftest` 的可以自检；3.x 阶段另外遵守统一的 `check/build/verify/install` 契约 |
| `scripts/selftest.sh` | 冒烟自检：所有脚本 `--help` 可用、`--selftest` 通过、shell 语法正确（`pre-commit` 与 CI 都跑它） |
| `requirements.txt` | 运行依赖（只有 `cryptography`）。不装也能用 `--help` 和大部分只读工具 |
| `private/` | **私有区，永不提交**（源数据、密钥、导出物、私有名单）。自带一个仅供本地记录的 git 仓库 |

源数据、密钥、导出内容一律留在 `private/`，从不进入版本控制。发布自检有两层：
公开的 [`scripts/check_no_secrets.sh`](scripts/check_no_secrets.sh)（通用模式）与本机的私有指纹名单，
`pre-commit` 与 CI 跑同一份；另有一道 [`scripts/selftest.sh`](scripts/selftest.sh) 冒烟自检防回归。

---

## 脚本总表

**2.x 段**（`--help` / `--selftest` 可用；参数未给时回退到环境变量）

| 脚本 | 作用 |
|---|---|
| `scripts/dump_mem.py` | 转储 32 位 WOW64 进程内存 → 扁平文件 + VA↔偏移索引（`--process` / `--out`） |
| `scripts/wxdec.py` | 用**你自己的密钥**解密整库（`--key-env` / `--key-hex`、`--page` / `--reserve`） |
| `scripts/export_2x.py` | 明文库 → 可读聊天日志 / CSV / 分会话 txt（`--plain` / `--out` / `--self`） |

**3.x 段**（统一四步契约）

| 脚本 | 作用 |
|---|---|
| `scripts/wxcom/` | 共享库：SQLCipher 3.x 往返、`DBInfo` 读写、`BytesExtra` 编解码、守卫式安装 |
| `scripts/wxdec3x.py` | 3.x 加解密与往返验证（按头部页数截断） |
| `scripts/wxrows.py` | 2.x `ChatMsg` → 3.x `MSG` 行迁移 |
| `scripts/wxmedia.py` | 媒体落位：`images` / `video` / `stickers` / `files` |
| `scripts/wxvoice.py` | 语音 → `MediaMSG0.db` 的 `Media` 表 |
| `scripts/wxstart.py` | `DBInfo.'Start Time'` 的安全读写（决定语音能不能播） |
| `scripts/wxsync.py` | 账号全库快照与差分（看清客户端自己写了什么） |
| `scripts/install_guard.sh` | 客户端干净退出 + 三道闸 + 强制备份，然后才替换 |

**4.x 段**

| 脚本 | 作用 |
|---|---|
| `scripts/wx4_decrypt.py` | 用**你自己的密钥**解密 4.x 库（含 page-1 HMAC 校验） |
| `scripts/wx4_attach_inventory.py` | 磁盘附件清单 + 双哈希（Windows 侧运行） |
| `scripts/wx4_attach_refs.py` | 从解密后的库里抽引用集（判断"还有没有人引用"） |
| `scripts/wx4_attach_audit.py` | 比对出重复组与孤立文件 |
| `scripts/wx4_attach_dedup.py` | 硬链接折叠（`plan` / `apply` / `verify` / `rollback`） |

---

## 本仓库**不做**什么（红线）

* **密钥必须来自你自己。** 你先得是**账号主人**，并在**你自己的设备**上以正常方式登录
  （扫码 / 密码）。本仓库**不涉及破解加密、不涉及撞库或猜测口令、不涉及绕过认证**，
  也不需要服务端侧的任何东西。**本文只讲格式、参数与迁移，不讲解密钥的取得步骤。**
* **不讲解密钥的获取方式。** 密钥处理工具只接受**你自己已有的**密钥
  （`--key-hex` / `--keys <文件>`）；请使用你自己的数据与合法途径。
* **不发布任何他人的数据。** 源数据、密钥、导出物永不进入版本控制。
* **不含任何绕过他人账号保护的内容。** 只针对**你自己**的账号与设备。

---

## 已知限制

* **已经消失的数据找不回来**：如果正文本身随磁盘/分区丢失，没有魔法。
* **当年没下载过的附件，任何副本都不存在**（我们扫过多个数据根、数十万个文件）。
* **手机备份**（`BAK_0_TEXT` / `BAK_0_MEDIA`）在密钥丢失时无法解开。
* **版本敏感**：本文参数在 WeChat 2.0.0.37 / 3.9.12.56 (x86) / 4.1.15.13 上实测；
  其它版本请自行验证。
* **界面自动化不是本项目的方向**：Weixin 4.x 是自绘渲染器，可自动化接口几乎为零，
  所以 4.x 那一步**只能手工点**（指南里写了点哪里）。

---

## 致谢与参考

| 参考 | 用在哪 | 许可 / 边界 |
|---|---|---|
| [`air846/WCDB`](https://github.com/air846/WCDB) | 4.x 的附件容器（V2）与表情格式 | 该仓库**无 LICENSE 文件** ⇒ 本项目**未分发其任何代码**，只参考其公开的格式描述 |
| `SQLCipher` | 加密参数的语义基准 | BSD |
| `zstandard` / `libzstd` | 解析 zstd 压缩的消息内容 | BSD |

上游代码**没有被复制进本仓库**（仅作本地参考）。若你认为本仓库某处内容侵犯了你的权利，
请开 issue —— 处置流程见 [`docs/appendix/takedown-runbook.md`](docs/appendix/takedown-runbook.md)。

---

## 适用边界与免责

* 仅适用于**你自己账号、你自己机器上**的恢复与迁移。密钥来自**账号主人自己在自己设备上的
  正常登录**（扫码 / 密码）；**不涉及破解加密、撞库或猜测口令、绕过认证**，也不需要服务端侧
  的任何东西。本文只讲格式、参数与迁移，不讲解密钥的取得步骤。
* 版本相关：本文参数在 WeChat 2.0.0.37 / 3.9.12.56 (x86) / 4.1.15.13 上实测，其它版本请自行验证。
* 客户端许可协议可能限制逆向。这里发布的是**文件格式与数据布局的知识**，
  以及**操作你自己数据的工具**；请读者自行判断当地法律。
* **不提供任何形式的担保**；不发布任何他人的数据。
