#!/usr/bin/env bash
# h723_git_setup.sh —— 一次性配置"开发推 H723PLC / 发布推 dcl-controller"的工具链
#
# 做三件事（全部可重复执行，幂等）：
#   ① `remote.pushdefault = origin`   ⇒ 裸 `git push` 落到 H723PLC（私人开发仓库）
#   ② 安装 pre-push 钩子             ⇒ 误推 dcl-controller 会被**拦截**（fail-closed）
#   ③ 修本机坏掉的 remote-tracking 引用（治标，见 memory §八）
#
# 用法: bash tools/h723_git_setup.sh
set -u
# ★★★ PATH 必须在**任何外部命令之前**导出（本机 bash 起来时 PATH 是空的）
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
export GIT_TERMINAL_PROMPT=0
cd "$(dirname "$0")/.."

DEV_REMOTE="origin"   # H723PLC        私人开发仓库
PUB_REMOTE="dcl"      # dcl-controller 对外公布新版本的官方仓库

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "$@"; }

git rev-parse --git-dir >/dev/null 2>&1 || die "不在 git 仓库里"

say "── ① 远程语义核对 ──"
for r in "$DEV_REMOTE" "$PUB_REMOTE"; do
  url="$(git remote get-url "$r" 2>/dev/null)" || die "远程 '$r' 不存在"
  case "$url" in
    *H723PLC*)        role="私人开发仓库（日常自动推）" ;;
    *dcl-controller*) role="对外发布仓库（人工上传）" ;;
    *)                role="未识别" ;;
  esac
  say "  $r = $url"
  say "       → $role"
done

say ""
say "── ② remote.pushdefault → $DEV_REMOTE ──"
OLD="$(git config --get remote.pushdefault || echo '(未设置)')"
say "  旧值: $OLD"
git config remote.pushdefault "$DEV_REMOTE"
say "  新值: $(git config --get remote.pushdefault)   ⇒ 裸 git push 现在推 H723PLC"
say "  ★ 对外仓库仍需人工: bash tools/h723_push.sh --with-dcl  （或 DCL_ALLOW_PUBLIC_PUSH=1 git push dcl main）"

say ""
say "── ③ 安装 pre-push 钩子 ──"
SRC="tools/git-hooks/pre-push"
[ -f "$SRC" ] || die "找不到 $SRC"
mkdir -p .git/hooks
cp "$SRC" .git/hooks/pre-push
chmod +x .git/hooks/pre-push 2>/dev/null || true
say "  已安装: .git/hooks/pre-push"
# ★ 自检直接喂参数给钩子（不走网络、不依赖 --dry-run 是否触发钩子）——判据必须能失败
say "  自检 A（目标=对外仓库，应被拦）:"
OUT="$(printf '' | sh .git/hooks/pre-push tcp "git@github.com:JimmyZ-zengmin/dcl-controller.git" 2>&1)"; RC=$?
echo "$OUT" | sed 's/^/    /'
[ "$RC" -ne 0 ] && say "  ✓ 拦住了 (RC=$RC)" || say "  ✗ 没拦住（RC=0）⇒ 钩子失效"
say "  自检 B（目标=开发仓库，应放行）:"
printf '' | sh .git/hooks/pre-push tcp "git@github.com:JimmyZ-zengmin/H723PLC.git" >/dev/null 2>&1
[ $? -eq 0 ] && say "  ✓ 放行 (RC=0)" || say "  ✗ 误拦开发仓库 ⇒ 钩子写错了"

say ""
say "── ④ 修 remote-tracking 引用（本机写 refs/remotes/** 会静默失败）──"
BR="$(git rev-parse --abbrev-ref HEAD)"
for r in "$DEV_REMOTE" "$PUB_REMOTE"; do
  sha="$(git ls-remote "$r" "refs/heads/$BR" 2>/dev/null | awk '{print $1}')"
  [ -z "$sha" ] && { say "  $r/$BR: 远程暂无该分支"; continue; }
  mkdir -p ".git/refs/remotes/$r"
  printf '%s\n' "$sha" > ".git/refs/remotes/$r/$BR"
  got="$(git rev-parse "$r/$BR" 2>/dev/null || echo '?')"
  [ "$got" = "$sha" ] && say "  ✓ $r/$BR → ${sha:0:7}" || say "  ✗ $r/$BR 写不进去 (got=$got)"
done

say ""
say "── 完成 ──"
say "  日常开发:  bash tools/h723_push.sh              # 只推 H723PLC"
say "  提交+推送: bash tools/h723_push.sh -m \"信息\" --path <显式路径...>"
say "  发布对外:  bash tools/h723_push.sh --with-dcl   # ★ 需人工确认"
