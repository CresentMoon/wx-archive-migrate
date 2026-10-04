#!/usr/bin/env bash
# selftest.sh —— 冒烟自检。改动脚本后先跑这个，再跑 check_no_secrets.sh。
#
# 检查三件事：
#   1. 每个 *.py 都能 `--help` 且退出码为 0（**这一条是回归闸门**：以前 2.x 的脚本
#      连 --help 都没有，或者在非 Windows 上 import 期就炸）；
#   2. 每个带 --selftest 的脚本自检通过（0 = 通过，77 = 缺依赖跳过，1 = 失败）；
#   3. 每个 *.sh 都能过 `bash -n`。
#
# 用法：
#     bash scripts/selftest.sh                    # 本机 python3（Linux / WSL）
#     PYTHON=python bash scripts/selftest.sh      # Windows 侧 Python **再跑一遍**
#
# ⚠️ 那第二遍不是可选项。平台相关的代码路径（`ctypes.WinDLL` 在模块顶层、
#    `ctypes.wintypes.DWORD` 的宽度、Windows 上加载不到 libcrypto）**只有换解释器
#    才会暴露** —— 在 WSL 里跑得再绿也覆盖不到。CI 里 ubuntu / windows 两个 job
#    跑的就是这两遍。
#
# 退出码：0 = 全通过；1 = 有失败。
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 9
PY="${PYTHON:-python3}"
FAIL=0
PASS=0

echo "== 0. 解释器 =="
"$PY" -c "import sys,platform; print('  %s' % sys.executable); print('  %s / %s' % (sys.platform, platform.machine()))" || {
  echo "  ✗ 找不到解释器：$PY"; exit 1; }

SKIP=0

echo "== 1. 每个脚本的 --help 必须可用 =="
# **必须带 `-X utf8`。** 原因（CI 上真踩过）：GitHub 的 windows runner 是 en-US，
# ANSI 代码页是 cp1252，**连中文都编不出来** ⇒ 不带 UTF-8 模式时，19 个脚本的
# `--help` 会**全部** UnicodeEncodeError。而这正是文档对 Windows 的要求
# （"一律 `python.exe -X utf8`"）。"中文控制台下别崩"这条保证由 1b 步负责。
for f in scripts/*.py; do
  if out=$("$PY" -X utf8 "$f" --help 2>&1); then
    PASS=$((PASS + 1))
    printf '  ✓ %s\n' "$f"
  else
    FAIL=1
    printf '  ✗ %s --help 失败（rc=%d）\n' "$f" "$?"
    printf '%s\n' "$out" | sed 's/^/      /' | head -5
  fi
done

echo "== 1b. --help 在**非 UTF-8 控制台**（GBK）下也必须能显示 =="
# 为什么单列一条：`--help` 是用户敲的第一条命令，它绝不能因为一个装饰性字符就崩。
# Windows 中文控制台默认 cp936，`↔ ⚠ ⇒ ✓` 这些字符**编不出来** ⇒ UnicodeEncodeError。
# 用 `PYTHONIOENCODING=gbk` 可以在**任何平台**复现这个失败，所以这条在 Linux 上也有效。
# （`--selftest` 不在此列：那一边按文档要求用 `-X utf8`。）
for f in scripts/*.py; do
  err=$(PYTHONIOENCODING=gbk "$PY" "$f" --help 2>&1 >/dev/null)
  rc=$?
  if [ "$rc" = "0" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=1
    printf '  ✗ %s --help 在 GBK 下失败（rc=%d）：%s\n' "$f" "$rc" \
      "$(printf '%s' "$err" | grep -oE "can't encode character '[^']+'" | head -1)"
    printf '      修法：help 文本里只用 GBK 能表示的字符（别用 ↔ ⚠ ⇒ ✓ ✗）。\n'
  fi
done

echo "== 2. 带 --selftest 的脚本自检 =="
# `-X utf8`：Windows 侧一律这么跑（见 CONTRIBUTING 与项目 AGENTS.md）。
# 在 Linux 上它等价于默认行为，不改变结果。
for f in scripts/*.py; do
  grep -q -- '--selftest' "$f" || continue
  out=$("$PY" -X utf8 "$f" --selftest 2>&1)
  rc=$?
  case "$rc" in
    0) PASS=$((PASS + 1)); printf '  ✓ %s\n' "$f" ;;
    77) SKIP=$((SKIP + 1)); printf '  ○ %s（跳过：%s）\n' "$f" "$(printf '%s' "$out" | head -1)" ;;
    *) FAIL=1; printf '  ✗ %s --selftest 失败（rc=%d）\n' "$f" "$rc"
       printf '%s\n' "$out" | sed 's/^/      /' | tail -5 ;;
  esac
done

echo "== 3. shell 脚本语法 =="
for f in scripts/*.sh .githooks/*; do
  [ -f "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then
    PASS=$((PASS + 1))
    printf '  ✓ %s\n' "$f"
  else
    FAIL=1
    printf '  ✗ %s bash -n 失败\n' "$f"
    bash -n "$f" 2>&1 | sed 's/^/      /' | head -5
  fi
done

find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null

echo
echo "通过 $PASS 项，跳过 $SKIP 项。"
if [ "$FAIL" = "0" ]; then
  echo "结果：冒烟自检通过。"
  exit 0
fi
echo "结果：**有失败**。"
exit 1
