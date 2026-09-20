#!/usr/bin/env bash
# dcl_publish.sh —— 把**过滤后的公开树**发布到 `dcl-controller`（对外仓库）
#
# ## 为什么需要它（用户 2026-09-19 明确要求）
#   此前 `dcl` 与 `origin`(H723PLC) 是**同一分支整树推送** ⇒ 公开仓里跟着上去了
#   `.workbuddy/`（撤回台账 P1~P33、铁律、血证、逐日复盘）与 `.tmpctl/`（临时脚本/产物）。
#   用户要求：**记忆文件只上传 H723PLC，不上传 dcl**。
#
# ## 做法（不重写历史，默认安全）
#   用**临时索引**从 HEAD 造一棵"只有白名单"的树 → 造一个提交（父 = 当前 `dcl/main`）
#   → 推给 `dcl/main`。因为父就是 `dcl/main`，所以这是 **fast-forward**，不需要 force。
#   公开树里从此**没有** `.workbuddy/`、`.tmpctl/`、`build*`、厂商 PDF 等。
#
#   ★ 注意（如实）：这一步让**当前树**干净，但 `.workbuddy/` 仍然留在 dcl 的**历史提交**里
#     （此前推过）。要连历史一起清掉，用 `--purge-history`（造一个无父提交 + force push，
#     **会丢弃公开仓的全部历史**）—— 那是对外不可逆操作，**必须显式指定**。
#
# ## 用法
#   bash tools/dcl_publish.sh --dry-run     # 只打印将发布的文件清单与统计
#   bash tools/dcl_publish.sh              # 发布（fast-forward）
#   bash tools/dcl_publish.sh --purge-history   # ★ 造无父提交 + force push（清历史）
#
# ## 白名单（公开面）与黑名单（内部）
#   公开: README.md build.sh CMakeLists.txt src/ tools/ docs/ examples/ ld/ startup/ cmake/
#   内部: .workbuddy/（记忆·台账·撤回）· .tmpctl/（临时）· build*/ · .la/
#         Hardware Official Documentation/（厂商 PDF，版权）
set -u
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
export GIT_TERMINAL_PROMPT=0
cd "$(dirname "$0")/.." || exit 1

PUB_REMOTE="dcl"
PUB_BRANCH="main"
DRY=0; PURGE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --purge-history) PURGE=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "✗ 未知参数: $a"; exit 2 ;;
  esac
done

# ── 白名单：只发布这些顶层路径（其余一律不进公开树）──────────────────────
KEEP='^(README\.md|build\.sh|CMakeLists\.txt|src/|tools/|docs/|examples/|ld/|startup/|cmake/)'

# ── 白名单里的**例外**（未 review 的草稿/内部评估，暂不公开）──────────────────
#   加一行的理由要写清；review 完删掉那一行即可（不需要改白名单）。
EXCLUDE='^(docs/PITCH-project-candidates\.md|docs/PLAN-expansion-headroom\.md)'

DEV_N="$(git ls-tree -r --name-only HEAD | wc -l)"   # ★ 必须在 read-tree 之前取（见下方诊断注释）
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
export GIT_INDEX_FILE="$TMPD/index"

git read-tree HEAD || exit 1
# 逐个把不在白名单里的**已跟踪文件**从临时索引里删掉
git ls-files -z | while IFS= read -r -d '' f; do
  case "$f" in
    */.gitkeep) ;;
  esac
  if printf '%s' "$f" | grep -Eq "$EXCLUDE"; then
    git update-index --force-remove -- "$f"      # 白名单里的例外（未 review 的草稿）
  elif ! printf '%s' "$f" | grep -Eq "$KEEP"; then
    git update-index --force-remove -- "$f"
  fi
done

TREE="$(git write-tree)" || exit 1
echo "== 公开树统计 =="
# ★ 这里**不能**用 `git ls-files` —— GIT_INDEX_FILE 已导出，它读的是**过滤后**的索引，
#   于是"开发仓文件数"会印成公开树的数（第一版就是这样：两行都是 435，看着像"过滤没生效"）。
echo "  开发仓 HEAD: $(git rev-parse --short HEAD)  文件数 $DEV_N"
echo "  公开树      : $TREE  文件数 $(git ls-tree -r --name-only "$TREE" | wc -l)"
echo "  ★ 被排除的内部路径（抽查）:"
for p in .workbuddy .tmpctl build "Hardware Official Documentation" .la; do
  n=$(git ls-tree -r --name-only "$TREE" | grep -c "^$p/" || true)
  echo "     $p/  → 公开树里 $n 个文件（期望 0）"
done
if [ "$DRY" = "1" ]; then
  echo
  echo "--dry-run: 公开树顶层 =="
  git ls-tree --name-only "$TREE" | sed 's/^/   /'
  exit 0
fi

# ── 造提交 ────────────────────────────────────────────────────────────────
MSG="publish: $(git log -1 --format='%h %s' | cut -c1-120)

（本提交由 tools/dcl_publish.sh 生成：公开树 = 开发仓 HEAD 去掉内部路径）
内部路径不发布: .workbuddy/（记忆·台账·撤回）· .tmpctl/ · build*/ · 厂商 PDF"

if [ "$PURGE" = "1" ]; then
  echo
  echo "★★ --purge-history: 造**无父提交**并 force push ⇒ 丢弃 dcl 的全部历史（清掉 .workbuddy 的旧副本）"
  C="$(git commit-tree "$TREE" -m "$MSG")" || exit 1
  git push --force "$PUB_REMOTE" "$C:refs/heads/$PUB_BRANCH" || exit 1
else
  PARENT="$(git rev-parse "$PUB_REMOTE/$PUB_BRANCH" 2>/dev/null || git rev-parse "$PUB_BRANCH" 2>/dev/null)"
  if [ -z "$PARENT" ]; then
    echo "!! 取不到 dcl/main 的父提交 ⇒ 先 git fetch $PUB_REMOTE"; exit 1
  fi
  C="$(git commit-tree "$TREE" -p "$PARENT" -m "$MSG")" || exit 1
  git push "$PUB_REMOTE" "$C:refs/heads/$PUB_BRANCH" || exit 1
fi

echo
echo "== 发布后核对（git ls-remote 是权威）=="
echo "  dcl/$PUB_BRANCH = $(git ls-remote "$PUB_REMOTE" "refs/heads/$PUB_BRANCH" | cut -f1)"
echo "  origin/main     = $(git ls-remote origin refs/heads/main | cut -f1)"
