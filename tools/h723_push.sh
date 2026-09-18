#!/usr/bin/env bash
# h723_push.sh —— 开发推送：**只推 H723PLC（私人开发仓库）**
#
# 两个远程的语义（用户 2026-09-18 明确）：
#   · origin = H723PLC       = **私人开发仓库**  ← 本脚本负责这个（日常自动推）
#   · dcl    = dcl-controller = **对外公布新版本的官方仓库** ← 由人工上传，本脚本默认不碰
#
# 用法:
#   bash tools/h723_push.sh                          # 把当前分支未推送的提交推到 H723PLC
#   bash tools/h723_push.sh -m "提交信息" --path src/step.c docs/x.md
#                                                    # 先按**显式路径**提交，再推送
#   bash tools/h723_push.sh --dry-run                # 只看会推什么，不动远程
#   bash tools/h723_push.sh --gate                   # 推之前先跑一遍 build.sh（六道闸门）
#   bash tools/h723_push.sh --fix-refs               # 顺手修本机坏掉的 remote-tracking 引用
#   bash tools/h723_push.sh --with-dcl               # ★发布用：额外推对外仓库（需显式确认）
#
# ★ 本脚本刻意遵守的三条本项目纪律:
#   1. **绝不用 `git add -A`** —— `-m` 必须同时给 `--path`（memory §八）
#   2. **提交信息走 `-F` 临时文件** —— `-m` 里的反引号会被 shell 命令替换掉（memory §八）
#   3. **"推没推上去"只用 `git ls-remote` 判定，不用 `git status`** ——
#      本机写 `refs/remotes/**` 静默失败（环境级缺陷）⇒ `git status` 永远误报 `[ahead N]`（memory §八）
set -u
# ★★★ PATH 必须在**任何外部命令之前**导出 —— 本机 bash 起来时 PATH 是空的，
#   连 `dirname` 都是 command not found（症状像"脚本坏了"）。见 memory §七速查。
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
export GIT_TERMINAL_PROMPT=0
cd "$(dirname "$0")/.."

DEV_REMOTE="origin"      # H723PLC —— 私人开发仓库
PUB_REMOTE="dcl"         # dcl-controller —— 对外发布仓库（人工）

MSG=""; DRY=0; GATE=0; FIXREFS=0; WITH_DCL=0; ALLOW_DIRTY=0
PATHS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m)            MSG="$2"; shift 2 ;;
    --path)        PATHS+=("$2"); shift 2 ;;
    --dry-run)     DRY=1; shift ;;
    --gate)        GATE=1; shift ;;
    --fix-refs)    FIXREFS=1; shift ;;
    --with-dcl)    WITH_DCL=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help)     sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "✗ 未知参数: $1（-h 看用法）"; exit 2 ;;
  esac
done

say() { echo "$@"; }
die() { echo "✗ $*" >&2; exit 1; }

# ---------- 0. 前置 ----------
git rev-parse --git-dir >/dev/null 2>&1 || die "不在 git 仓库里"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "HEAD" ] && die "当前是 detached HEAD，拒绝自动推送（先 git checkout main）"

for r in "$DEV_REMOTE"; do
  git remote get-url "$r" >/dev/null 2>&1 || die "远程 '$r' 不存在（先跑 bash tools/h723_git_setup.sh）"
done
DEV_URL="$(git remote get-url "$DEV_REMOTE")"
say "开发仓库 $DEV_REMOTE = $DEV_URL"
say "分支     $BRANCH     HEAD = $(git rev-parse --short HEAD)"

# ---------- 1. 可选：构建闸门 ----------
if [ "$GATE" = "1" ]; then
  say ""
  say "── 构建闸门 (bash build.sh) ──"
  # ★ 不用管道把门：退出码是最后一条命令的（memory §八 / RULES §11.8）
  bash build.sh > /tmp/h723_push_build.log 2>&1
  RC=$?
  if [ "$RC" -ne 0 ]; then
    tail -30 /tmp/h723_push_build.log
    die "构建失败 (RC=$RC) ⇒ 拒绝推送（日志 /tmp/h723_push_build.log）"
  fi
  say "✓ build.sh RC=0"
fi

# ---------- 2. 可选：提交 ----------
if [ -n "$MSG" ]; then
  [ "${#PATHS[@]}" -eq 0 ] && die "-m 必须同时给 --path（本脚本禁止 git add -A）"
  say ""
  say "── 提交 ──"
  git add -- "${PATHS[@]}" || die "git add 失败"
  if git diff --cached --quiet; then
    say "⚠ 指定路径没有暂存到改动 ⇒ 跳过提交"
  else
    git diff --cached --stat
    # ★ 用 -F：消息里的反引号经 -m 会被 shell 吃掉（本项目踩过）
    TF="$(mktemp)"; printf '%s\n' "$MSG" > "$TF"
    git commit -F "$TF" || { rm -f "$TF"; die "提交失败"; }
    rm -f "$TF"
  fi
fi

# ---------- 3. 干净度检查 ----------
if [ "$ALLOW_DIRTY" != "1" ]; then
  if [ -n "$(git status --porcelain)" ]; then
    say ""
    git status --short
    die "工作区有未提交改动 ⇒ 用 --path/-m 提交，或 --allow-dirty 显式跳过（改动不会丢，但不会被推送）"
  fi
fi

# ---------- 4. 用 ls-remote 取远程真值（不信 tracking ref） ----------
REMOTE_SHA="$(git ls-remote "$DEV_REMOTE" "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}')"
HEAD_SHA="$(git rev-parse HEAD)"
if [ -z "$REMOTE_SHA" ]; then
  say "远程还没有 refs/heads/$BRANCH（首次推送）"
  RANGE="HEAD"
else
  say "远程 $DEV_REMOTE/$BRANCH = ${REMOTE_SHA:0:7}"
  if [ "$REMOTE_SHA" = "$HEAD_SHA" ]; then
    say "✓ 已是最新，无需推送"
    RANGE=""
  else
    # 远程提交是否在本地历史里（判断能不能 fast-forward）
    if git merge-base --is-ancestor "$REMOTE_SHA" "$HEAD_SHA" 2>/dev/null; then
      RANGE="$REMOTE_SHA..HEAD"
    else
      die "远程有本地没有的提交（分叉）⇒ 拒绝自动推送，请先人工 git pull --rebase"
    fi
  fi
fi

if [ -n "${RANGE:-}" ]; then
  say ""
  say "── 将推送的提交 ──"
  if [ "$RANGE" = "HEAD" ]; then git log --oneline -20 HEAD; else git log --oneline "$RANGE"; fi
fi

if [ "$DRY" = "1" ]; then
  say ""
  say "(--dry-run) 到此为止，没有动远程"
  exit 0
fi

# ---------- 5. 推 ----------
if [ -n "${RANGE:-}" ]; then
  say ""
  say "── git push $DEV_REMOTE $BRANCH ──"
  git push "$DEV_REMOTE" "$BRANCH" || die "推送 $DEV_REMOTE 失败"
fi

# ---------- 6. 核对（权威判据 = ls-remote） ----------
NEW="$(git ls-remote "$DEV_REMOTE" "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}')"
if [ "$NEW" = "$HEAD_SHA" ]; then
  say "✓ 核对通过：$DEV_REMOTE/$BRANCH = $(git rev-parse --short HEAD)（ls-remote 实读）"
  DEV_OK=1
else
  say "✗ 核对失败：远程=${NEW:0:7} 本地=${HEAD_SHA:0:7}"
  DEV_OK=0
fi

# ---------- 7. 可选：推对外仓库 ----------
if [ "$WITH_DCL" = "1" ]; then
  say ""
  say "── ★ 对外发布：$PUB_REMOTE (dcl-controller) ──"
  if [ "$BRANCH" != "main" ]; then
    say "⚠ 对外仓库只发布 main；当前分支 $BRANCH ⇒ 跳过"
  else
    DCL_ALLOW_PUBLIC_PUSH=1 git push "$PUB_REMOTE" main || die "推送 $PUB_REMOTE 失败"
    PUB="$(git ls-remote "$PUB_REMOTE" "refs/heads/main" 2>/dev/null | awk '{print $1}')"
    [ "$PUB" = "$HEAD_SHA" ] && say "✓ 核对通过：$PUB_REMOTE/main = $(git rev-parse --short HEAD)" \
                             || say "✗ 核对失败：$PUB_REMOTE/main = ${PUB:0:7}"
  fi
fi

# ---------- 8. 可选：修本机 tracking ref（治标，见 memory §八） ----------
if [ "$FIXREFS" = "1" ]; then
  say ""
  say "── 修 remote-tracking 引用（本机写 refs/remotes/** 会静默失败）──"
  for r in "$DEV_REMOTE" "$PUB_REMOTE"; do
    git remote get-url "$r" >/dev/null 2>&1 || continue
    sha="$(git ls-remote "$r" "refs/heads/$BRANCH" 2>/dev/null | awk '{print $1}')"
    [ -z "$sha" ] && continue
    mkdir -p ".git/refs/remotes/$r"
    printf '%s\n' "$sha" > ".git/refs/remotes/$r/$BRANCH"
    got="$(git rev-parse "$r/$BRANCH" 2>/dev/null || echo '?')"
    if [ "$got" = "$sha" ]; then say "✓ $r/$BRANCH → ${sha:0:7}"
    else say "✗ $r/$BRANCH 写不进去（got=$got）"; fi
  done
fi

[ "${DEV_OK:-0}" = "1" ] || exit 1
exit 0
