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

# ── ★★★ 放行令牌（2026-09-22 修）────────────────────────────────────────────
#   `.git/hooks/pre-push` 拦截**一切**指向 dcl-controller 的推送，只放行设了
#   `DCL_FROM_PUBLISH_SCRIPT=1` 的那些 —— 防的是"整树直推把 .workbuddy/ 带进公开仓"。
#   ★ 缺陷（本次实测踩到）：钩子 2026-09-20 20:36 装上，**而本脚本从来没有设过这个变量**
#     ⇒ 两半接口名对不上 ⇒ **发布路径从装上那天起就是死的**（dcl 远程停在 `1575dc3`）。
#     症状是 `git push` 报 `✗ pre-push 拦截 …`，而提示让人去跑……**正是本脚本自己**。
#   ⇒ 教训：**"守卫"与"被守卫的动作"必须放在同一处或有一个共同的测试**；
#     只靠两边各写一遍字符串，一定会漂移。
export DCL_FROM_PUBLISH_SCRIPT=1

# ── ★★★ 取远程 HEAD：**必须用 `git ls-remote`，不能用本地 `dcl/main`** ────────────
#   血证（2026-09-22 实测）：本仓 `.git/packed-refs` 的 mtime = **2026-09-14 15:55**，
#   而**所有 remote-tracking ref 只存在于 `packed-refs`**（`.git/refs/remotes/` 是空目录）⇒
#     · `git fetch dcl main` 报 `+ 4a283b3...1575dc3 main -> dcl/main (forced update)`，
#       但**松散引用写不进去**、packed-refs 也不改 ⇒ 目录建了又被抹掉；
#     · `git rev-parse dcl/main` **静默回落到 09-14 的旧值 `089fd97`**（远程实际 `1575dc3`）。
#   ⇒ 造出的提交祖先错 ⇒ push 被**非快进**拒绝（本次实测撞到）。
#   ★★ 同一故障让 `git status` / `git log <remote>/main` 显示**完全错误的领先/落后**
#      —— 这正是"本地工作与 git 脱节"的机械成因之一。
#   ⇒ **纪律：凡与远程比对/取远程状态，一律用 `git ls-remote`（它直连远程，不经过本地引用）。**
remote_head() { git ls-remote "$PUB_REMOTE" "refs/heads/$PUB_BRANCH" 2>/dev/null | cut -f1; }

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
# ── 把不在白名单里的**已跟踪文件**从临时索引里删掉 ────────────────────────────
# ★★ 2026-09-22 修：原实现**逐文件** spawn 两个进程（`grep` 判 + `git update-index` 删）
#   ⇒ 484 个跟踪文件 ≈ **1000 次进程创建** ⇒ 在 Windows/Git Bash 上**跑不完**
#     （实测 `--dry-run` 被掐在 `docs/STAGE3-REPORT.md`，`bash -x` 日志 765 行全是 trace、
#      零实际输出 —— 看起来像"卡死"，其实是慢到超过时限）。
#   ⇒ 这是"**本地工作与 git 严重脱节**"的一个隐藏成因：发布路径在这台机器上走不到底。
#   ⇒ 改成：两个 `grep -z` 单趟输出 + **一次** `git update-index --stdin -z`：
#      ~1200 次 spawn → 3 次（实测 **1.9 s**）。
#   ★ 两条 `git ls-files -z` **都在 `update-index` 之前**跑完，所以两次看到的是同一份完整清单。
git ls-files -z | grep -zvE "$KEEP"    > "$TMPD/rm_nokeep"   # 不在白名单 ⇒ 移除
git ls-files -z | grep -zE  "$EXCLUDE" > "$TMPD/rm_excl"     # 白名单内的例外 ⇒ 移除
cat "$TMPD/rm_nokeep" "$TMPD/rm_excl" | git update-index --force-remove -z --stdin || exit 1
echo "  移除: $(tr -cd '\0' < "$TMPD/rm_nokeep" | wc -c) 个（非白名单）+ $(tr -cd '\0' < "$TMPD/rm_excl" | wc -c) 个（例外）"

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
  PARENT="$(remote_head)"           # ★ 权威来源见上方 remote_head() 的注释
  if [ -z "$PARENT" ]; then
    echo "!! 取不到 dcl/$PUB_BRANCH 的远程 HEAD ⇒ 检查网络 / SSH 权限"; exit 1
  fi
  echo "  父提交 = $(printf '%s' "$PARENT" | cut -c1-7)  （取自 git ls-remote，权威）"
  # 先把远程那个提交的 object 取回来（否则 commit-tree -p 指向不存在的对象）
  git fetch -q "$PUB_REMOTE" "$PUB_BRANCH" 2>/dev/null || true
  C="$(git commit-tree "$TREE" -p "$PARENT" -m "$MSG")" || exit 1
  git push "$PUB_REMOTE" "$C:refs/heads/$PUB_BRANCH" || exit 1
fi

echo
echo "== 发布后核对（git ls-remote 是权威）=="
# ★★ 原来这里**只打印不判**（2026-09-22 改）。打印出来的数字人不看就等于没有判据，
#    而"发布到底成没成"恰恰是最需要能失败的一条 —— 参见本次钩子把发布挡死两天没人发现。
REMOTE_SHA="$(git ls-remote "$PUB_REMOTE" "refs/heads/$PUB_BRANCH" | cut -f1)"
LOCAL_SHA="$(git rev-parse "$C")"
echo "  刚推的提交 = $LOCAL_SHA"
echo "  dcl/$PUB_BRANCH = $REMOTE_SHA"
echo "  origin/main     = $(git ls-remote origin refs/heads/main | cut -f1)"
echo "  公开树文件数     = $(git ls-tree -r --name-only "$C" | wc -l)"
# 失败判据 1: 远程必须等于刚推的提交
if [ "$REMOTE_SHA" != "$LOCAL_SHA" ]; then
  echo "✗ **发布失败**: 远程 dcl/$PUB_BRANCH 未更新到刚推的提交"; exit 1
fi
# 失败判据 2: 公开树里绝不许出现内部路径（"过滤生效"的反向判据）
for p in .workbuddy .tmpctl build "Hardware Official Documentation" .la; do
  n=$(git ls-tree -r --name-only "$C" | grep -c "^$p/" || true)
  if [ "$n" != "0" ]; then
    echo "✗ **发布失败**: 公开树里出现 $p/ 共 $n 个文件（过滤没生效！）"; exit 1
  fi
done
echo "✓ 发布成功：远程已更新，且公开树里 5 个内部路径全为 0"
