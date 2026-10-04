# 附加步骤：附件重复与孤立的审计 + 硬链接折叠

> 给**已经完成 4.x 迁移**的人看的一篇操作手册：先查清 `xwechat_files` 里哪些附件内容重复、
> 哪些文件在库里已经没有任何线索，再把重复项折叠成 NTFS 硬链接。
> 全程**不改路径、不改文件名、不碰任何 `.db`**，每一条都能回滚。
> 工具全部在 [`scripts/`](../scripts) 里，本文只讲命令与验收。

## 目标

迁移结束后，附件树里通常躺着同一份内容的多个副本：同一张图在多个会话目录各存一份，
文件附件出现 `X.pdf` / `X(1).pdf` / `X(1)(1).pdf`。目标是把这些副本**收敛成一份物理数据**：

* 算出**按 inode 口径**的真实可回收量（不是文件个数口径，见「判定规则」）；
* 列出**孤立文件**（库里读不到任何引用线索的文件）—— 本指南**只报告、不删除**；
* 用硬链接折叠重复：**路径条目数量一个不少**，物理占用下降，并留一份撤销日志。

已在本项目实测跑通的规模（仅供对照，不是预期值）：**折叠 13,414 个名字 / 6,687 组，
路径条目 189,187 条零变化，物理占用 −22.897 GB，可回滚**。

## 前提

1. **客户端必须已干净退出。** 硬链接会改变 inode，客户端正在写文件时不要动。
   4.x 干净退出后 `-wal` 不会归零，判据用「**进程消失 + `-wal` 的 mtime 不再变化**」。
2. **4.x 的库能被读出来。** 审计的「孤立」判定依赖库里的引用集，
   所以要用你自己的密钥文件解密：`scripts/wx4_decrypt.py <库> --keys <文件>`。
   打不开的库对应的账号，其附件**一律不参与判定**（那是「没查」，不是「孤立」）。
3. **同一个 NTFS 卷。** 硬链接不能跨卷；`xwechat_files` 整棵树要落在同一个卷上。
4. **不要用 Syncthing 同步折叠后的附件树。** Syncthing 不保硬链接，
   一同步就退化成「每个链接一份」，把省下来的空间原样还回去。
   备份请选**保硬链接**的工具；`fsutil hardlink list <路径>` 可以只读查看名字关系。
5. **`plan` 与 `apply` 必须在同一平台、同一台机器上跑。**
   Linux 的 drvfs 与 Windows 原生读出的 `st_ino` 恒定差 2，工具只信自己现场 `stat` 的结果。
   先在一小部分目录上试点（见步骤 4 的 `--limit-groups`），再放全量。

## 步骤

### 1. 盘清单 + 双哈希（Windows 侧跑）

给每个文件算两个 md5：`md5_raw`（落盘字节）与 `md5_norm`（解开容器后的内容）。
Windows 原生 `python.exe` 读这套目录比 WSL 的 `/mnt/c` 快得多。

```bat
python.exe -X utf8 scripts\wx4_attach_inventory.py ^
  --root "C:\<数据根>\xwechat_files" ^
  --out  "C:\_wx4_pathprobe\attach-inventory.tsv" ^
  --threads 4
```

* **只读**，不写、不改、不删任何被扫描的文件。`--threads` 默认 `4`；
  `--limit N` 是调试用，只处理前 N 个文件。
* `C:\_wx4_pathprobe\` 是 Windows 侧的探测目录（Linux 侧即 `/mnt/c/<探测目录>/`），
  清单放在数据根之外，免得自己扫自己。
* 验收：输出 `文件数 N，扫描根 …`、结尾 `完成 N 个文件，… 类型分布: [...]`；
  TSV 首行是 `#relpath size mtime md5_raw md5_norm kind nlink inode`，共 8 列，
  `nlink` / `inode` **必须非 0**。
* `kind` 的取值：`plain` / `xor-a0` / `container-v1` / `container-v2` / `raw` / `error`。
  只有 `xor-a0` 的 `md5_norm` 与 `md5_raw` 不同；容器类退回 `md5_raw`。
  `size = -1` 的行是读失败，后面的审计会自动跳过。

### 2. 抽库里引用集（Linux 侧跑）

从解密后的 4.x 库抽出两个集合：32 位 hex 的 **id**（图片/视频/文件内容 md5）与**文件名**。

```bash
python3 scripts/wx4_attach_refs.py \
  --root /mnt/c/<数据根>/xwechat_files \
  --out  /tmp/wx4audit/refs.json.gz \
  --work /tmp/wx4audit/db \
  --keys <你的密钥文件>
```

* `--work` 默认 `/tmp/wx4audit/db`；`--keys` 是**必填**参数（本脚本会自己解密库）；明文只落在 `--work`，
  加 `--no-cache` 可强制重新解密、不用缓存。输出 `refs.json.gz`：gzip JSON，含 `ids`、`names`、`meta`。
* 验收：结尾打印 `ids=… names=…`；`meta.accounts_with_refs` 里应该出现你要处理的所有账号。
  没出现的账号说明它的库没读成功 —— **该账号不要进入步骤 4**，用 `--accounts` 排除掉。

### 3. 比对出报告

```bash
python3 scripts/wx4_attach_audit.py \
  --inv  /mnt/c/<探测目录>/attach-inventory.tsv \
  --refs /tmp/wx4audit/refs.json.gz \
  --out  reports/attach-audit
```

产出（目录由 `--out` 指定）：

| 文件 | 内容 |
|---|---|
| `stats.json` | 全部计数与字节数（含按账号、按区域的孤立统计） |
| `dup-groups.csv` | 重复组：组号、文件数、**不同 inode 数**、keeper、可回收字节、是否跨封装 |
| `dup-files.csv` | 重复明细，带 `role=keep`/`dup`、`inode`、`nlink`、`indexed` —— **步骤 4 的输入** |
| `dup-and-orphan.csv` | 既重复、又孤立的文件（最保守的清理候选） |
| `orphans.csv` | 全部孤立（含 `orphan_kind=link-only`：数据另有引用，只是这条链接没被索引） |
| `orphans-true.csv` | 真孤立，带 `cls` 分类：`A` 有索引孪生 / `B` 仅孤立间重复 / `C` 完全独一份 |

* 验收：终端应打印「精确重复（同 md5_raw）」与「规范化重复（同 md5_norm）」两组数字，
  以及两者之差「跨封装」的组数；`stats.json` 里的 `dup_exact_fully_hardlinked_groups`
  就是**已经硬链接、折叠也不省空间**的组数。
* 本指南到这一步为止：**孤立文件一个都不删**，那是独立决策。

### 4. 出折叠计划（dry-run，默认不动手）

```bat
python.exe -X utf8 scripts\wx4_attach_dedup.py plan ^
  --dup-files "reports\attach-audit\dup-files.csv" ^
  --root "C:\<数据根>\xwechat_files" ^
  --out  "plan.json" ^
  --inv  "C:\_wx4_pathprobe\attach-inventory.tsv" ^
  --areas msg ^
  --accounts "<你确认可查的账号目录>"
```

* `--areas` 默认 `msg`，只处理路径第 2–4 段含该目录名的组；空串 = 全部。
  `--accounts` 默认空 = 全部，**强烈建议显式写上**账号目录，免得把同根下打不开库的账号卷进来。
  可选 `--inv` 交叉核对 size；`--limit-groups N` 只取前 N 组，**试点就靠它**。
* 验收：终端打印「组 N / 待折叠 M 个 / 预期释放 X」与一行跳过统计
  （区域外 / 已同 inode / 跨封装（字节不同） / 文件不存在 / 不合格）。

### 5. 执行折叠（不加 `--yes` 就是 dry-run）

```bat
python.exe -X utf8 scripts\wx4_attach_dedup.py apply ^
  --plan "plan.json" --manifest "manifest.json" --limit-groups 20
python.exe -X utf8 scripts\wx4_attach_dedup.py apply ^
  --plan "plan.json" --manifest "manifest.json" --yes
```

* **默认 dry-run**：不加 `--yes` 只把将要折叠的每一条写进 manifest，不改任何文件。
* 只在 `md5_raw` **逐字节相同**时才折叠；跨封装（`md5_norm` 相同、`md5_raw` 不同）一律跳过。
* 顺序是 `os.link` → `os.replace`，全程不存在「文件不见了」的窗口；绝不先删后建。
  `--no-content-check` 会跳过执行前的头尾采样指纹复核，**除非你有别的理由，不要用**。
* 单个文件失败只跳过它并记进 manifest 的 `errors`，不中断整批。
* 验收：终端打印「完成：处理 G/G 组，折叠 M 个，X GB；跳过 S，异常 E」；
  `manifest.json` 里 `errors` 为空、`folded` 每条的 `new_inode` 与 keeper 一致；
  最后确认目录里**没有** `<名>.hl.tmp` 残留。

### 6. 验收

```bat
python.exe -X utf8 scripts\wx4_attach_dedup.py verify --manifest "manifest.json"
```

* 逐条现场 `stat`：每个 `dup` 与它的 keeper 必须**同 inode、同 size**，且每组只有一个 inode。
* 退出码 `0` = 全通过；`1` = 有异常或仍有组存在多个 inode。
* `verify` / `rollback` 必须与 `plan` / `apply` 在**同一平台**上跑，否则路径与 `st_ino` 都对不上。
* 想再确认物理占用，用 `fsutil hardlink list <路径>` 看名字关系，并统计路径条目数与
  路径条目总字节 —— 应与折叠前**逐字节相等**（只有不同 inode 数下降）。

## 对应脚本

| 脚本 | 作用 | 关键参数 |
|---|---|---|
| [`wx4_attach_inventory.py`](../scripts/wx4_attach_inventory.py) | 盘清单 + `md5_raw`/`md5_norm` 双哈希，输出 TSV（Windows 侧快） | `--root` `--out`（必填）、`--threads`（默认 4）、`--limit`（调试） |
| [`wx4_attach_refs.py`](../scripts/wx4_attach_refs.py) | 从解密后的库抽 id 与文件名 → `refs.json.gz` | `--root` `--out`（必填）、`--work`（默认 `/tmp/wx4audit/db`）、`--keys`（默认仓库根 `KEYS_4x.txt`）、`--no-cache` |
| [`wx4_attach_audit.py`](../scripts/wx4_attach_audit.py) | 比对出重复组 / 孤立文件 CSV 与 `stats.json` | `--inv` `--refs` `--out`（全部必填） |
| [`wx4_attach_dedup.py`](../scripts/wx4_attach_dedup.py) | 折叠成硬链接，四个子命令 | 见下 |

`wx4_attach_dedup.py` 的四个子命令：

| 子命令 | 必填 | 可选 |
|---|---|---|
| `plan` | `--dup-files` `--root` `--out` | `--inv` `--areas`（默认 `msg`）`--accounts`（默认空）`--limit-groups` |
| `apply` | `--plan` `--manifest` | `--yes`（不加 = dry-run）、`--limit-groups`、`--no-content-check` |
| `verify` | `--manifest` | — |
| `rollback` | `--manifest` | `--yes`（不加 = dry-run） |

`-X utf8` 是 Windows 侧的必需项：不显式指定编码，中文输出会变成 ANSI 字节，`不同 inode` 看起来像 `同 inode` —— 据此下判断一定是错的。

## 判定规则

1. **判重必须按 inode。** 客户端自己已经对大二进制建了硬链接（`hardlink.db` 就是它自己的
   去重账本，官方迁移也这么干）。同一 inode 上的第 2、3 个名字折叠过来
   **不省任何空间**，只有搬走最后一个名字才真正释放。口径是
   `(不同 inode 个数 − 1) × 大小`；按文件个数算会**高估一倍**。
   本项目实测有 **2,576 组已经全部硬链接**，可回收为 0。
2. **不能按文件名判重。** 同名 ≠ 同内容：`X.pdf` 与 `X(1).pdf` 完全可能是两份毫不相干的文件。
   按文件名顺手合并会**直接覆盖数据**。分组只能由**内容哈希**驱动，
   并且硬链接只在 `md5_raw` **逐字节相同**时才做。
3. **路径条目必须零变化。** 客户端按「**目录 + 文件名**」找文件，而目录由消息时间
   （`msg/file/<年-月>/`）或接收会话（`msg/attach/<会话md5>/…`）决定，两个都改不了。
   所以「一个内容只留一个文件名」在大部分重复上**结构上做不到**；能做到的上限是
   **一份物理数据 + N 个路径名**，那就正好是硬链接。验收标准：折叠前后路径条目数与
   路径条目总字节**逐字节相等**。副作用是同组共用一套 mtime 与 Windows 只读属性，
   被折叠的名字继承 keeper 的这两项，**内容零变化**。

**为什么不再往下走一步 —— 改写库里的路径？** 收益是 **0 字节**（空间在硬链接这一步已经全部拿到），只多影响约 3% 的文件名；而代价是篡改 `message_resource` 里记录的本地名，等于给「**手机聊天记录 → 电脑**」的合并功能喂我们无法预测其行为的输入（这个合并必然消费 PC 端已有的文件名与资源记录）。风险与收益完全不成比例，所以**主动放弃**。

## 回滚

`rollback` 从 keeper 把每个被折叠的名字重建为独立文件，并恢复记录的 `old_mtime`：

```bat
python.exe -X utf8 scripts\wx4_attach_dedup.py rollback --manifest "manifest.json"

python.exe -X utf8 scripts\wx4_attach_dedup.py rollback --manifest "manifest.json" --yes
```

* **默认 dry-run**：不加 `--yes` 只数一遍会还原多少条。
* 已经在回滚后又被改动、或已不再与 keeper 同 inode 的条目会被**跳过**并计数，不会硬写。
* `manifest` 必须在**同一平台**上回滚，换平台会直接中止（退出码 2）。
* 回滚**不重建原来的 inode**：它把 keeper 的字节复制成一份新文件、替换掉那个名字，再设回 `old_mtime`。
  真正的兜底仍然是折叠前的完整备份。

## 常见失败

| 症状 | 原因 | 怎么办 |
|---|---|---|
| 实测释放量远小于「重复文件数 × 大小」 | 按文件个数算的口径。客户端已经硬链接了一部分（实测 2,576 组） | 看 `plan` 的 `skipped_already_hardlinked` 与 `expected_saved_bytes`，那才是按 inode 算的真值 |
| `apply` 报 `WinError 5`，目录里留 `<名>.hl.tmp` | 目标文件带 Windows READONLY，属性**随 inode 共享**，`os.replace` 失败且残骸也删不掉 | 工具会先清只读位再替换；重跑 `apply --yes` 会自动清理历史残骸。结束后确认没有 `.hl.tmp` 残留 |
| `apply` / `rollback` 立刻退出，提示平台不一致 | `plan` 与 `apply` 不在同一平台 —— Linux 与 Windows 的 `st_ino` 恒定差 2 | 在同一平台、同一台机器上重跑 `plan` 再 `apply` |
| `verify` 报「不同 inode」或某组仍有多个 inode | keeper 在执行后被替换/重写过，inode 变了 | 对该 manifest `rollback --yes` 还原，重新 `plan` |
| 折叠后个别图片/视频客户端打不开，或显示异常/一直转圈 | ① 该组是「跨封装」重复：`md5_norm` 相同但落盘字节不同（例如明文一份 + XOR 加密的 `_W.dat` 一份），共享 inode 后加密那份的字节被明文替换；② 折叠时有进程在写这棵树 | ① 工具默认按 `md5_raw` 跳过这类组 —— 检查 `dup-files.csv` 是否被手工改过、是否误用了 `--no-content-check`，对该组 `rollback`；② 客户端彻底退出、确认 `-wal` 的 mtime 不再变化后重跑 `verify` |

---

继续看：[`guide-4x.md`](guide-4x.md)（4.x 迁移主流程）·
[`detail/4x-migration.md`](detail/4x-migration.md)（4.x 附件布局与迁移实测细节）·
[`appendix/pitfalls.md`](appendix/pitfalls.md)（踩坑检查表）。
