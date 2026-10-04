#!/usr/bin/env bash
# install_guard.sh —— 等客户端**干净退出**，确认三道闸都过了，才执行安装命令。
#
# 为什么必须"干净退出"
# --------------------
# 客户端运行时，MSG0.db / MediaMSG0.db 等库有**未检查点的 WAL**（`-wal` 文件）。
# 直接替换主库会**静默丢掉这些写入** —— 我们实测见过 WAL 里出现触及
# `sqlite_sequence` / `Name2ID` / `MsgTalkerIdTypeSeqIndex` 的帧，那意味着
# **客户端在那次会话里刚收到的一条真实新消息**；它之所以能保住，就是因为
# 等到了干净检查点（反之就会被静默丢掉，且没有任何报错）。
# 托盘右键 → 退出微信 时，SQLite 会自己检查点并删掉 `-wal`/`-shm`，此时才是安全窗口。
#
# 三道闸
# ------
#   ① 进程不在：探测 **采样 3 次取最大值**，返回空/非数字一律当"还在跑"，
#      必须**连续 4 轮**都是 0 才算真退出（pwsh 偶尔会打嗝返回空串，
#      一次误判就会在客户端重启的瞬间动手 —— 这个坑我们踩过）。
#   ② WAL 清干净：所有目标库（`--wal` 指定，或 `--scan-root` 扫出来的全部 `*.db`）
#      的 `-wal` 必须**不存在或 0 字节**。
#   ③ 动手前再确认一次进程没复活。
#   任一条不满足就**放弃，且不改动任何文件**。
#
# 用法
# ----
#   install_guard.sh [选项] [--] <命令...>
#
#   # 最常见：包住一次语音安装
#   bash install_guard.sh --scan-root "<DATA_ROOT>/<ACCOUNT>/Msg" -- \
#        python3 wxvoice.py install --wechat-dir "<DATA_ROOT>" --account <ACCOUNT> ...
#
#   bash install_guard.sh --dry-run          # 只演练等待与闸门，不执行命令
#   bash install_guard.sh                    # 无参数：打印用法，退出 0
#
# 选项
# ----
#   --process <名字>     进程名，默认 WeChat（Windows 上也可以写 Weixin）
#   --wal <库路径>       需要检查 -wal 的库，可重复
#   --scan-root <目录>   递归找出目录下所有 *.db 作为检查目标，可重复
#   --timeout <秒>       等待上限，默认 1800
#   --stable <轮数>      需要连续多少轮确认进程不在，默认 4
#   --probe <命令>       自定义"打印进程数"的命令（非 Windows/WSL，或想自己控制时用）
#   --dry-run            过完闸门只打印，不执行命令
#   -h | --help          打印本用法
set -u

PROCESS="WeChat"
WAL_TARGETS=()
SCAN_ROOTS=()
TIMEOUT=1800
STABLE=4
PROBE_CMD=""
DRY_RUN=0

usage() { sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------- 参数
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)   usage; exit 0 ;;
    --process)   PROCESS="${2:?--process 需要值}"; shift 2 ;;
    --wal)       WAL_TARGETS+=("${2:?--wal 需要值}"); shift 2 ;;
    --scan-root) SCAN_ROOTS+=("${2:?--scan-root 需要值}"); shift 2 ;;
    --timeout)   TIMEOUT="${2:?--timeout 需要值}"; shift 2 ;;
    --stable)    STABLE="${2:?--stable 需要值}"; shift 2 ;;
    --probe)     PROBE_CMD="${2:?--probe 需要值}"; shift 2 ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --)          shift; break ;;
    -*)          echo "未知选项：$1" >&2; usage; exit 2 ;;
    *)           break ;;
  esac
done
CMD=("$@")

if [ ${#CMD[@]} -eq 0 ] && [ "$DRY_RUN" = "0" ]; then
  echo "没有给要执行的命令。"
  usage
  exit 0
fi

# ---------------------------------------------------------------- 进程探测
PS_BIN=""
for c in pwsh.exe powershell.exe pwsh powershell; do
  if command -v "$c" >/dev/null 2>&1; then PS_BIN="$c"; break; fi
done

probe_once() {
  if [ -n "$PROBE_CMD" ]; then
    eval "$PROBE_CMD" 2>/dev/null | tr -d '\r\n '
    return
  fi
  # 注意：这一整串必须是纯 ASCII —— PowerShell 5.1 读非 ASCII 会解析失败。
  "$PS_BIN" -NoProfile -Command \
    "(Get-Process -Name '$PROCESS' -ErrorAction SilentlyContinue | Measure-Object).Count" \
    2>/dev/null | tr -d '\r\n '
}

count() {
  # 采样 3 次取**最大值**；空/非数字一律当 1（"还在跑"，保守） —— 避免误判。
  local best=0 v
  local i
  for i in 1 2 3; do
    v="$(probe_once)"
    case "$v" in
      ''|*[!0-9]*) v=1 ;;
    esac
    [ "$v" -gt "$best" ] && best="$v"
    sleep 0.5
  done
  echo "$best"
}

walsz() { [ -f "$1-wal" ] && stat -c%s "$1-wal" 2>/dev/null || echo "-"; }

if [ -z "$PS_BIN" ] && [ -z "$PROBE_CMD" ]; then
  echo "找不到 PowerShell（pwsh.exe / powershell.exe）。" >&2
  echo "非 Windows 环境请用 --probe '<打印进程数的命令>' 自己提供探测方式。" >&2
  exit 2
fi

echo "== 目标进程：$PROCESS    探测方式：${PROBE_CMD:-$PS_BIN}"
echo "== 当前进程数（采样取最大）：$(count)"

# ---------------------------------------------------------------- 闸①：等到连续 N 轮不在
echo "== 闸①：等待连续 $STABLE 轮确认进程已退出（上限 ${TIMEOUT}s）…"
ok=0
elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
  n="$(count)"
  if [ "$n" = "0" ]; then ok=$((ok + 1)); else ok=0; fi
  if [ $((ok % STABLE)) -eq 0 ] && [ "$ok" -gt 0 ]; then
    echo "   已连续 $ok 轮为 0（约 ${elapsed}s）"
  fi
  [ "$ok" -ge "$STABLE" ] && break
  sleep 1
  elapsed=$((elapsed + 3))
done
if [ "$ok" -lt "$STABLE" ]; then
  echo "！！ 超时：${TIMEOUT}s 内没有确认到干净退出，放弃（未改动任何文件）。" >&2
  exit 1
fi
echo "   确认已退出（约 ${elapsed}s）"

sleep 3
if [ "$(count)" != "0" ]; then
  echo "！！ 客户端又起来了 —— 放弃（未改动任何文件）。" >&2
  exit 2
fi

# ---------------------------------------------------------------- 闸②：WAL 清干净
TARGETS=("${WAL_TARGETS[@]+"${WAL_TARGETS[@]}"}")
for r in "${SCAN_ROOTS[@]+"${SCAN_ROOTS[@]}"}"; do
  [ -d "$r" ] || continue
  while IFS= read -r f; do TARGETS+=("$f"); done < <(find "$r" -name '*.db' -type f 2>/dev/null)
done

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "== 闸②：没有指定 --wal/--scan-root，**跳过 WAL 检查**（安全性下降，建议补上）"
else
  echo "== 闸②：检查 ${#TARGETS[@]} 个库的 -wal"
  FAIL=0
  for f in "${TARGETS[@]}"; do
    sz="$(walsz "$f")"
    if [ "$sz" = "-" ]; then
      echo "   ✓ $(basename "$f")：-wal 已删除（干净检查点）"
    elif [ "$sz" = "0" ]; then
      echo "   ✓ $(basename "$f")：-wal = 0 B"
    else
      echo "   ✗ $(basename "$f")：-wal = ${sz} B —— **没有干净检查点**（可能是强杀）"
      FAIL=1
    fi
  done
  if [ "$FAIL" = "1" ]; then
    echo "！！ 放弃安装：请重新用「托盘右键 → 退出微信」正常退出，不要任务管理器强杀。" >&2
    exit 3
  fi
fi

# ---------------------------------------------------------------- 闸③：动手前再确认
if [ "$(count)" != "0" ]; then
  echo "！！ 松开闸门的一刻客户端又启动了 —— 放弃。" >&2
  exit 4
fi
echo "== 闸③：进程仍不在 ✓"

# ---------------------------------------------------------------- 执行
if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "== --dry-run：闸门全部通过，按计划本应执行："
  printf '   %s\n' "${CMD[@]+"${CMD[@]}"}"
  exit 0
fi

echo
echo "== 三道闸都过了，开始执行命令 =="
printf '   %s\n' "${CMD[@]}"
echo
"${CMD[@]}"
rc=$?

echo
echo "== 命令退出码：$rc"
echo "== 请确认上面出现过「备份 …」并记下了「还原命令：cp -a …」。"
echo "== 没有备份字样就先别开客户端，回看工具的 install 输出。"
echo "== 现在可以启动客户端了。"
exit "$rc"
