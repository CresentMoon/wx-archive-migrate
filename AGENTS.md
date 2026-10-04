# 本项目的工作纪律

> 仓库内容见 [`README.md`](README.md) 与 [`docs/README.md`](docs/README.md)。
> 这里只写**在本仓库里干活时必须遵守的几条**。

## 1. 过程产物只落在三个地方

* 仓库里的 **`private/`**（随 Syncthing 备份到 NAS，**永不推送**）
* **`/tmp`**（临时，可丢）
* Windows 侧**唯一一个**探测目录 **`C:\_wx4_pathprobe\`**

**不要往 home 根放东西**，也**不要新开 `D:\_xxx` 实验目录** ——
`D:` 上已经积了 16 个 `_*` 目录，散开了就再也没人搞得清哪个是哪轮的。

## 2. 动微信数据之前

* **先在 manifest 里登记**：要创建/移动/删除的每个路径都记下来，并给出回退方法。
  参考 `private/tools/wx4_fix_stickers.py`（`stage` / `quarantine-bubbles` / `teardown` 三段式）。
* **只增不改**：目标已存在就跳过，绝不覆盖。
* **客户端要干净退出**再动数据根；`.db` 的回写必须等 `-wal` 归零。
* **不覆盖、不删除**任何我们没有明确重建过的东西。

## 3. 两条真踩过的坑：跨 Linux↔Windows 边界时，路径与编码都会变形

**① 别把 Windows 路径喂给 Linux 工具。**
在 Linux 下的 Python 里 `os.makedirs(r'C:\_wx4_pathprobe\backup')`
**不会**去 C 盘 —— 它会在**当前目录**建一个名字里带反斜杠的畸形目录。
（本项目仓库里真的出现过一次。）工具里一律先过一遍 `to_wsl_path()`。

**② Windows 侧那两个壳默认都不是 UTF-8，而 `$PROFILE` 和环境变量前缀都靠不住。**
`VAR=value python.exe …` 前缀到不了 Windows 侧（只转发 `WSLENV`）；机器级变量
（`setx … /M`）要等 **`wsl --shutdown`** 才注入 interop 进程；`pwsh` 被代理调用时带
`-NoProfile`，而 `[Console]::OutputEncoding` 又是进程内状态、无法继承。
⇒ 跑 Windows 侧工具时**在命令行里显式指定**：
`python.exe -X utf8 …` / `pwsh.exe -NoProfile -Command '[Console]::OutputEncoding=[Text.Encoding]::UTF8; …'`

⚠️ **乱码会骗人**（`不同inode` 看着像 `同inode`，本项目差点因此把 199 个安全残骸
误判成数据损坏）⇒ **中文结果一律写文件（`encoding='utf-8'`）再从 Linux 读。**

## 4. 界面自动化：能不碰就不碰

Weixin 4.x 是 Qt 5.15 + 自绘渲染器，**UIA 只暴露 2 个 Pane**，没有任何可用的自动化接口。
`private/tools/_dsh_*.ps1`（`PrintWindow` 截图 + 合成点击 + 滚轮）是**最后手段**：
它会让用户的窗口自己乱动，**优先让用户自己操作**。

## 5. 文档纪律

* 每个结论都带 **实测确认 / 推断 / 未验证** 标签；**不许把推断写成实测**。
* **结论被推翻时，把旧结论连同"错在哪"一起留着**，不要直接删掉 —— 见
  `private/docs/02-derived-key-memory-sweep.md` §10.2、`private/PROCESS_LOG.md` 阶段 8。
  这类记录比正确结论更省后来的时间。
* 大批量改仓库前先 `systemctl --user stop syncthing`，改完再 `start` 并核对 NAS 侧
  没有滞留的 `.syncthing.*.tmp`。
* **动了 `scripts/` 下的脚本，先跑 `bash scripts/selftest.sh`**（`pre-commit` 也会跑它）。
  新脚本要有 `--help`，并把重活放进函数里 —— 模块顶层不能有平台依赖，
  否则非 Windows 上连 `--help` 都打不开。能自检的加 `--selftest`（0 / 1 / 77）。

## 6. 测 Windows 侧脚本：**换解释器**，别以为 WSL 里测不了

WSL 互操作可以直接跑 Windows 侧 Python —— 这跟 §3 的"环境变量前缀过不去"是两回事：

```bash
python.exe -X utf8 scripts/dump_mem.py --selftest      # 单个脚本
PYTHON=python.exe bash scripts/selftest.sh             # 整套冒烟自检
```

**平台相关的代码路径只有换解释器才会暴露。** 2026-10 的教训：我先在 WSL 里看到
`SKIP: 非 Windows`，就写下"Windows 内核路径本次未验证"—— 其实只要调一次 `python.exe`
就能验，是**我没试**。后来补跑，一次性暴露两件事：`dump_mem.py` 的句柄/结构体改动
在真机上是好的（`SELFTEST PASSED`），而 `sweep*.py` 在 Windows 上**加载不到 libcrypto**
（wheel 是静态 OpenSSL）⇒ 自检应当报 `SKIP`(77) 而不是 `FAIL`(1)。

CI 里 `.github/workflows/smoke.yml` 有 ubuntu / windows 两个 job 跑同一份自检，
本地照上面两条命令对齐。
