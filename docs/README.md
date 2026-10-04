# 文档索引

**怎么读**：先从仓库根 [`README.md`](../README.md) 弄清这是什么，然后按你要做的那一步读**指南**；
指南里遇到"为什么是这样"再翻**细节**；**附录**是踩坑与证据，动手前建议扫一眼。

| 层 | 目录 | 给谁看 | 特点 |
|---|---|---|---|
| **指南** | `docs/guide-*.md` | 要动手的人 | 命令级、可复制；每步都有「验收」 |
| **细节** | `docs/detail/` | 想搞清原理的人 | 格式、参数、布局、以及"为什么这样做" |
| **附录** | `docs/appendix/` | **动手前 & 后来的自己** | 坑、已证伪结论、证据索引、风险处置。**不是使用说明** |

---

## 指南（按顺序做）

| 文件 | 一句话 | 对应脚本 |
|---|---|---|
| [guide-2x.md](guide-2x.md) | 让旧版客户端**能登录**一次，并把数据根固化下来 | `dump_mem.py`、`wxdec.py`、`export_2x.py` |
| [guide-3x.md](guide-3x.md) | 把 2.x 的历史做成 **3.9.12.56 能读的形状** | `wxdec3x.py`、`wxrows.py`、`wxmedia.py`、`wxvoice.py`、`wxstart.py`、`wxsync.py`、`install_guard.sh` |
| [guide-4x.md](guide-4x.md) | 用官方「加载历史聊天记录」迁进 **4.x**（主要靠手工点） | `wx4_decrypt.py`（其余环节无脚本） |
| [guide-attachments.md](guide-attachments.md) | 附件**重复 / 孤立**审计 + 硬链接折叠 | `wx4_attach_inventory.py`、`wx4_attach_refs.py`、`wx4_attach_audit.py`、`wx4_attach_dedup.py` |

---

## 细节

| 文件 | 内容 |
|---|---|
| [detail/launch-and-version-gate.md](detail/launch-and-version-gate.md) | 「版本过低」的**三层机制**（自带升级器 / `MinVersion` 自我登出 / 一次性设备豁免）、完整操作步骤、§7 是**一次性机会的作业清单** |
| [detail/sqlcipher-params.md](detail/sqlcipher-params.md) | SQLCipher 的**参数与页布局**、回写三原则、验证配方 |
| [detail/row-mapping.md](detail/row-mapping.md) | 2.x `ChatMsg` → 3.x `MSG` 的**逐列语义**；怎么用真客户端自写的行反推列语义 |
| [detail/media-layout.md](detail/media-layout.md) | **按消息类型**逐个说明媒体落在哪、是否编码、路径记在哪；`BytesExtra` 的两层 protobuf |
| [detail/voice-start-time.md](detail/voice-start-time.md) | ★ 语音不在文件系统里；决定它能否播放的是 `DBInfo.'Start Time'`（**时间闸门**） |
| [detail/4x-migration.md](detail/4x-migration.md) | 4.x 的完整实测：官方迁移流程与验收、附件布局、字段级重扫、表情「懒加载取回」机制 |
| [detail/attachments.md](detail/attachments.md) | 附件审计的**细节**：4.x 附件命名与容器形态（含 V2 容器逐字段结构）、判重口径、孤立判定 |
| [detail/schema-3x.sql](detail/schema-3x.sql) | 3.x 库的完整 DDL —— **用它自己建目标库**，不必依赖任何人的模板库 |

---

## 附录（**不是使用说明**）

| 文件 | 内容 |
|---|---|
| [appendix/pitfalls.md](appendix/pitfalls.md) | 3.x 写库的**操作级**检查表：症状 → 原因 → 做法（行边界、WAL、预分配截断、往返比对、正则误伤…） |
| [appendix/dead-ends.md](appendix/dead-ends.md) | **路线级**死路与已证伪结论（含总表）。这是我们花最多时间买来的东西 |
| [appendix/evidence-index.md](appendix/evidence-index.md) | 结论 → 复现路径的追溯表：每条断言对应哪一步实测 |
| [appendix/takedown-runbook.md](appendix/takedown-runbook.md) | 这类项目被投诉时的处置手册，以及**不要做**什么（含 force-push 的真相） |

> **证据标签**贯穿全部文档：**实测确认** / **推断** / **未验证** / **已证伪**。
> 「实测确认」= 我们在本机复现并核对过；「推断」= 有机制支撑但没做对照。
> **被推翻的旧结论不会被删掉**，而是留删除线 + 原因（见 `appendix/dead-ends.md` §7）。
>
> 文档里用占位符：`<N0>`/`<N1>` = 消息行号边界，`<L*>` = 单条消息的 `localId`，
> `<D*>` = 日期，`<TS_MS>` = 毫秒时间戳。**真实值只留在私有区**。

---

## 脚本

**统一 CLI 契约**（3.x 与 4.x 阶段的工具都遵守）：

```
<tool> check   [--dry-run]     # 只读。报告「将要改什么」，默认动作
<tool> build                   # 生成到工作目录，不碰客户端
<tool> verify                  # 全页 HMAC + 可用区往返 + integrity_check + 逐行核对
<tool> install [--yes]         # 需要过 install_guard；先备份，再替换，并打印还原命令
```

任何 `install` 都必须：① 确认客户端已**干净退出**；② 把原库复制到备份目录；③ 打印备份路径与还原命令。

> ℹ️ **每个脚本都支持 `--help`**（重活都在函数里，模块顶层没有平台依赖，所以在 Linux/WSL
> 上也能打印出来）。带 `--selftest` 的脚本可以自检：`0` = 通过、`1` = 失败、
> `77` = 跳过（缺 `cryptography` 或平台不符）。要一次跑完所有自检：
> `bash scripts/selftest.sh`。
>
> **2.x 阶段的脚本**（`dump_mem.py` / `wxdec.py` / `export_2x.py`）用**参数 + 环境变量兜底**
> 的方式取输入（`--dump` / `WX_DUMP_DIR` 等）；不在这份表里的 2.x 工具属于内存分析环节，
> 用法同样看 `--help`。
>
> 4.x 阶段的脚本在 **Windows 侧 Python** 下运行；从 WSL 调用请用 `python.exe -X utf8`。

| 脚本 | 作用 |
|---|---|
| [`check_no_secrets.sh`](../scripts/check_no_secrets.sh) | **发布前自检**：凭据 / 个人路径 / 账号标识 / 手机号 / 危险文件类型 + `py_compile`。**两层**：公开脚本只放**通用模式**，具体指纹在本机私有名单里 —— 规则里塞具体值，规则自己就成了泄露源 |
| [`wxcom/`](../scripts/wxcom/) | 共享库：SQLCipher 3.x 往返、`DBInfo` 读写、`BytesExtra` 编解码、守卫式安装 |
| [`wxdec3x.py`](../scripts/wxdec3x.py) | 3.x 加解密：用**你自己的密钥**、`--salt-from <原库>`、按头部页数截断、往返自检 |
| [`wxrows.py`](../scripts/wxrows.py) | 2.x `ChatMsg` → 3.x `MSG` 行迁移，从 `schema-3x.sql` 建表 |
| [`wxmedia.py`](../scripts/wxmedia.py) | 媒体落位，按类型分：`images` / `video` / `stickers` / `files` |
| [`wxvoice.py`](../scripts/wxvoice.py) | 语音 → `MediaMSG0.db` 的 `Media` 表 |
| [`wxstart.py`](../scripts/wxstart.py) | `DBInfo.'Start Time'` 的安全读写（`--show` / `--set` / `--auto`），默认只读 |
| [`wxsync.py`](../scripts/wxsync.py) | 账号全库 snapshot / diff —— 「客户端到底写了什么」的通用差分工具 |
| [`install_guard.sh`](../scripts/install_guard.sh) | 等客户端**干净退出** + 三道闸，然后才替换 |
| [`export_2x.py`](../scripts/export_2x.py) | 明文库 → 可读聊天日志 / CSV / 分会话 txt |
| [`wx4_decrypt.py`](../scripts/wx4_decrypt.py) | 4.x 库解密（页级 HMAC 校验）；**需要你自己提供密钥文件** |
| [`wx4_attach_inventory.py`](../scripts/wx4_attach_inventory.py) | 磁盘附件清单 + 双哈希（Windows 侧运行） |
| [`wx4_attach_refs.py`](../scripts/wx4_attach_refs.py) | 从库中抽附件引用集，用来判断"还有没有被引用" |
| [`wx4_attach_audit.py`](../scripts/wx4_attach_audit.py) | 比对出重复组与孤立文件，出 CSV / JSON 报告 |
| [`wx4_attach_dedup.py`](../scripts/wx4_attach_dedup.py) | 硬链接折叠：`plan` / `apply` / `verify` / `rollback`（默认 dry-run） |
| [`selftest.sh`](../scripts/selftest.sh) | **冒烟自检**：每个脚本 `--help` 可用、带 `--selftest` 的能通过、shell 语法正确。改脚本后先跑它。要装依赖：`pip install -r requirements.txt`（只有 `cryptography`） |

---

## 三条路线（按你的目标选一条）

| 路线 | 目标 | 读什么 |
|---|---|---|
| **A —— 只要能读的数据** | 把旧库解开、导出成可读文本 / CSV | `guide-2x.md` → `export_2x.py` |
| **B —— 要客户端本身可用** | 让旧客户端能登录、能打开旧库 | `guide-2x.md`（注意 §前提里的**一次性**） |
| **C —— 要把它迁进现代客户端** | 在 A/B 之后，再走 `guide-3x.md` → `guide-4x.md` | `guide-3x.md`、`guide-4x.md` |

---

## 免责

仅用于**恢复你自己账号、你自己设备上的数据**。不发布任何他人的数据或凭据。
**本文档不讲解数据库密钥的获取方式**；密钥处理工具只接受**你自己已有的**密钥。
微信是腾讯公司的产品，本项目与腾讯无任何关联。
