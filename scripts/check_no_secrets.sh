#!/usr/bin/env bash
# check_no_secrets.sh —— 发布前自检：这个仓库里不该有任何密钥、个人路径、账号标识、
#                       实验指纹，或第三方二进制。
#
# 用法：
#     bash scripts/check_no_secrets.sh [目录 ...]     # 扫工作区（默认动作）
#     bash scripts/check_no_secrets.sh --history      # 扫**所有提交的所有文件**（CI 用）
#
# 退出码：0 = 干净；1 = 有命中。
# **不要加白名单绕过命中** —— 请把内容改成占位符或删掉。
#
# 规则只写在这一处：.githooks/pre-commit 与 .github/workflows/no-secrets.yml 都调用本脚本。
# （曾经 CI 里复制过一份模式串，结果自匹配了自己 —— 所以不再复制。）
#
# 为什么值得这么严：本项目用到的派生密钥**不可轮换**（同一账号重新登录会复现同一把），
# 一旦进过 git 历史就等于永久泄露，force-push 也救不回来。指纹类信息同理。
#
# 目录约定：`private/` 是**永不提交**的私有区（实验脚本、转储、原库副本、密钥），
# 由 .gitignore 挡住，因此本脚本必须跳过它 —— 那里的东西本来就是敏感原文，
# 扫它只会满屏误报，把真正的信号淹掉。历史模式天然看不到它（它从未被提交）。
set -u

# ---------------------------------------------------------------- 规则
# 每条规则是 "说明|正则"（ERE）。命中即失败。
RULES=(
  '64 位十六进制串（疑似密钥）|[0-9a-fA-F]{64}'
  '微信账号数据目录名（形如 <id>_<6位hex>）|[A-Za-z0-9_-]{4,}_[0-9a-f]{6}([^0-9a-f]|$)'
  'wxid / 群 ID|wxid_[a-z0-9]{6,}|[0-9]{6,}@chatroom'
  'Windows 个人绝对路径|C:\\Users\\[A-Za-z]|D:\\WeChat|D:\\_wx|D:\\_dump'
  'WSL 个人目录|/mnt/[cd]/_wx|/mnt/[cd]/WeChat|/mnt/[cd]/_dump'
  '手机号（中国大陆，恰好 11 位）|(^|[^0-9])1[3-9][0-9]{9}([^0-9]|$)'
  '本机主机名（形如 XXX-PC）|[A-Z][A-Z0-9]{2,}-PC([^A-Za-z]|$)'
  '已作废的旧 Sequence 写法（每会话计数器）|Sequence[[:space:]]*=[[:space:]]*s[,)]'
  # ---- 以下是「实验指纹」里**可以写成通用模式**的部分 ----
  # 具体值（账号、主机名、本次数据集的真实规模与起止日期、真实消息时间）**刻意不写在这里**：
  # 规则里塞进具体值，等于让规则自己变成泄露源 —— 而本脚本被 SKIP 排除，
  # 那种泄露永远查不出来。它们放在本机的 private/secrets-denylist.tsv，见下面的加载逻辑。
  '真实聊天时间戳（带时分秒 —— 请改写成 <D1> 之类占位符或虚构值）|20(1[0-9]|2[0-5])-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}'
  # 只匹配「20xx 年」这种**叙述形式**：二十年前的 4 位数到处都有（hex 常量 `2000`、
  # `0x20000`、msgtype `2000`、SDK 端口），所以**不能**用裸 `20[0-9]{2}` 去扫。
  '数据集的年代写成具体年份（请改用「十年前」这类相对说法）|20(1[0-9]|2[0-9])[[:space:]]*年'
)

# ---- 本机私有名单（第二层）------------------------------------------------------
# private/ 永不提交，所以 CI 拿不到它：CI 只跑通用规则，本机 pre-commit 跑「通用 + 本机」。
# 名单格式：每行 `说明 <TAB> ERE`，`#` 开头为注释（文件里自带完整说明）。
DENY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/private/secrets-denylist.tsv"
DENY_N=0
if [ -f "$DENY" ]; then
  while IFS=$'\t' read -r d p; do
    case "${d:-}" in ''|\#*) continue ;; esac
    [ -z "${p:-}" ] && continue
    RULES+=("$d|$p"); DENY_N=$((DENY_N+1))
  done < "$DENY"
fi

# 规则载体文件：它们必然包含上面这些模式串，扫自己只会永远失败。
SKIP=(
  ':(exclude)scripts/check_no_secrets.sh'
  ':(exclude).github/workflows/no-secrets.yml'
)
# 工作区模式要跳过的目录：机器/平台产物，以及**私有区**（私有区从未提交，不是本脚本的对象）。
EXCLUDES=(--exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=venv
          --exclude-dir=.venv --exclude-dir=_stage --exclude-dir=_publish
          --exclude-dir=private
          --exclude=check_no_secrets.sh --exclude=no-secrets.yml)
# find 用的同一份名单（几处 find 复用），必须是**数组**，否则那行会被当成命令执行。
FPRUNE=( \( -path '*/.git' -o -path '*/__pycache__' -o -path '*/venv'
          -o -path '*/.venv' -o -path '*/_stage' -o -path '*/_publish'
          -o -path '*/private' \) -prune )

# ---------------------------------------------------------------- 历史模式
if [ "${1:-}" = "--history" ]; then
  cd "$(git rev-parse --show-toplevel)" || exit 9
  echo "== 历史自检（所有提交的所有文件）=="
  FAIL=0
  for c in $(git rev-list --all); do
    for rule in "${RULES[@]}"; do
      desc="${rule%%|*}"; pat="${rule#*|}"
      hits=$(git grep -InE -- "$pat" "$c" -- . "${SKIP[@]}" 2>/dev/null | head -10)
      if [ -n "$hits" ]; then
        echo "  ✗ [$c] ${desc}"
        echo "$hits" | sed 's/^/      /'
        FAIL=1
      fi
    done
  done
  if [ "$FAIL" = "0" ]; then
    echo "  ✓ 历史里没有任何命中（共 $(git rev-list --all | wc -l) 个提交）"
    exit 0
  fi
  echo
  echo "结果：**历史里命中了**。请注意：改写历史并不能撤回已经公开过的内容，"
  echo "但如果这些提交还没推送，请在推送前用 git rebase/filter-repo 处理掉。"
  exit 1
fi

# ---------------------------------------------------------------- 工作区模式
DIRS=("$@")
[ ${#DIRS[@]} -eq 0 ] && DIRS=(".")

echo "== 规则命中 =="
echo "   （通用规则 ${#RULES[@]} 条，其中本机私有名单 $DENY_N 条$([ "$DENY_N" = 0 ] && echo ' —— 未找到 private/secrets-denylist.tsv，只跑了通用层'))"
FAIL=0
for rule in "${RULES[@]}"; do
  desc="${rule%%|*}"
  pat="${rule#*|}"
  hits=$(grep -rInE "${EXCLUDES[@]}" -- "$pat" "${DIRS[@]}" 2>/dev/null)
  if [ -n "$hits" ]; then
    FAIL=1
    echo "  ✗ ${desc}"
    echo "$hits" | sed 's/^/      /' | head -20
  else
    echo "  ✓ ${desc}"
  fi
done

echo "== 不该出现的文件类型 =="
BIN=$(find "${DIRS[@]}" "${FPRUNE[@]}" -o \
        -type f \( -iname '*.db' -o -iname '*.db-wal' -o -iname '*.db-shm' \
                   -o -iname '*.bin' -o -iname '*.exe' -o -iname '*.dll' \
                   -o -iname '*.pak' -o -iname '*KEYS*' -o -iname '*.key' \
                   -o -iname '*.pem' -o -iname 'snap_*.json' \) -print 2>/dev/null)
if [ -n "$BIN" ]; then
  FAIL=1
  echo "  ✗ 发现不该提交的文件："
  echo "$BIN" | sed 's/^/      /' | head -20
else
  echo "  ✓ 没有数据库/转储/二进制/密钥文件"
fi

echo "== 语法检查 =="
PYFAIL=0
while IFS= read -r f; do
  python3 -m py_compile "$f" 2>/dev/null || { echo "  ✗ py_compile 失败: $f"; PYFAIL=1; }
done < <(find "${DIRS[@]}" "${FPRUNE[@]}" -o -name '*.py' -type f -print 2>/dev/null)
[ "$PYFAIL" = "0" ] && echo "  ✓ 所有 .py 编译通过" || FAIL=1
find "${DIRS[@]}" "${FPRUNE[@]}" -o -name '*.sh' -type f -print 2>/dev/null | while read -r f; do
  bash -n "$f" || echo "  ✗ bash -n 失败: $f"
done
find "${DIRS[@]}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null

echo
if [ "$FAIL" = "0" ]; then
  echo "结果：干净（可以提交）"
  exit 0
fi
echo "结果：**有命中，拒绝提交**。请改成占位符或删除对应内容。"
exit 1
