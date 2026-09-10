#!/usr/bin/env bash
# sweep_freq.sh — H723 频率天花板扫描 (改 N → 编译 → 烧录 → LA 外部测频)
#
# 为什么用 LA 而不是 SWD:
#   SWD 在"CPU 跑不住"时会失步/读回全 0, 会把"芯片其实活着"误判为"挂了"
#   (本项目已踩过: SWD 报 300~340MHz 断崖, LA 实测却是 450MHz 能跑)。
#   只有把 PA8 引到 LA 上才是**独立于固件的物理证据**。
#
# 接线: LA CH4 ← H723 PA8, GND 共地
#
# 原理: TIM2 ARR = (TIMxCLK/1e6)*100, 而 TIMxCLK 由**假定的**频率算出,
#   所以 PA8 实际周期与假定值成反比 → 可反推真实 CPU:
#       cpu_actual = cpu_assumed * 100us / period_measured
#
# 用法 (在项目根执行):
#   bash sweep_freq.sh 80 90 92 94        # 扫 400 / 450 / 460 / 470 MHz
#   bash sweep_freq.sh 80 --dur 1.0       # 指定采集时长 (默认 0.5s)
#
# ★扫描会修改 src/clock.h 的 CLK_PLL1_DIVN1 / CLK_HPRE_CODE。
#   H9 修正 (2026-09-10): 原版只靠最后一行文字提醒恢复 —— Ctrl-C / 编译失败 /
#   烧录失败都会把被扫描值留在工作树里 (污染 git status, 且下次构建用的是扫描值,
#   而人以为还是 400MHz)。现在改成 **备份 + trap 自动恢复**: 无论正常结束、报错还是
#   被 Ctrl-C, 退出时都会把 clock.h 还原并重新构建默认固件。
#   N 的取值: CPU = 5MHz × N。建议 N 取 4 的倍数 → CPU/HCLK/APB 全为整数。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
CMAKE="/c/Espressif/tools/cmake/4.0.3/bin/cmake.exe"
NINJA="C:/Espressif/tools/ninja/1.12.1/ninja.exe"
PY="C:/Users/min/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
CLOCK_H_WIN="$(cygpath -w "$HERE/src/clock.h")"
CLOCK_H="$HERE/src/clock.h"
BACKUP="$HERE/build/clock.h.sweepbak"

restore_clock() {
    if [ -f "$BACKUP" ]; then
        cp "$BACKUP" "$CLOCK_H"
        echo
        echo "── 已还原 src/clock.h (H9: 不再把扫描值留在工作树里) ──"
        grep -nE "#define CLK_PLL1_DIVN1|#define CLK_HPRE_CODE" "$CLOCK_H" | sed 's/^/   /'
        # 同步重建默认固件, 避免"下一次构建用的是扫描值"
        if "$CMAKE" --build "$(cygpath -w "$HERE/build")" >/dev/null 2>&1; then
            echo "   已按还原后的配置重新构建 (build/dcl_h723.hex 恢复正常落点)"
        else
            echo "   ⚠️ 还原后重建失败, 请手动 bash build.sh 检查"
        fi
        rm -f "$BACKUP"
    fi
}

DUR="0.5"
NS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dur) DUR="$2"; shift 2 ;;
        *)     NS+=("$1"); shift ;;
    esac
done
[ ${#NS[@]} -eq 0 ] && {
    echo "用法: bash sweep_freq.sh <N...> [--dur s]"
    echo "  CPU = 5MHz × N   (N=80 → 400MHz, N=92 → 460MHz)"
    exit 1
}

# 首次运行需要先配置 CMake
if [ ! -f "$HERE/build/CMakeCache.txt" ]; then
    echo "[首次] 配置 CMake…"
    bash "$HERE/build.sh" >/dev/null 2>&1
fi

# ★ H9: 先把 clock.h 备份好, 再注册恢复钩子 —— 之后任何退出路径都会还原
cp "$CLOCK_H" "$BACKUP"
trap 'restore_clock' EXIT INT TERM

set_n() {   # $1=N  $2=HPRE 分频码
    "$PY" - "$1" "$2" "$CLOCK_H_WIN" <<'PYEOF'
import sys, re
n, h, f = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(f, encoding='utf-8').read()
s = re.sub(r'#define CLK_PLL1_DIVN1\s+\d+u[^\n]*',
           '#define CLK_PLL1_DIVN1  %su          /* sweep: 5MHz x %s */' % (n, n), s)
s = re.sub(r'#define CLK_HPRE_CODE\s+\d+u[^\n]*',
           '#define CLK_HPRE_CODE   %su   /* sweep */' % h, s)
open(f, 'w', encoding='utf-8').write(s)
PYEOF
}

echo "  CPU(MHz) | HCLK | 结果"
echo "-----------+------+------------------------------------------------------"
for N in "${NS[@]}"; do
    MHZ=$(( N * 5 ))
    HCLK=$(( MHZ / 2 ))
    set_n "$N" 8

    if ! "$CMAKE" --build "$(cygpath -w "$HERE/build")" >/dev/null 2>&1; then
        printf "  %8s | %4s | 编译失败 (可能触发 clock.c 的规格断言)\n" "$MHZ" "$HCLK"
        continue
    fi

    pyocd flash -t stm32h723xx -O connect_mode=under-reset \
        "$(cygpath -w "$HERE/build/dcl_h723.hex")" >/dev/null 2>&1

    # ★Git Bash 路径必须转成 Windows 路径再喂给 Windows Python, 否则脚本根本没跑,
    #   输出里没有判据关键字 → 会被误判成"✅ 运行" (已踩)
    OUT=$("$PY" "$(cygpath -w "$HERE/la_tick_freq.py")" --cpu "$MHZ" --ch 4 \
          --rate 4000000 --dur "$DUR" 2>&1)

    if ! echo "$OUT" | grep -q "测量结果\|跳变太少\|几乎不翻转\|无任何跳变"; then
        printf "  %8s | %4s | ⚠️  测量未完成 (脚本报错)\n" "$MHZ" "$HCLK"
        echo "$OUT" | tail -3 | sed 's/^/        /'
        continue
    fi
    if echo "$OUT" | grep -q "跳变太少\|几乎不翻转\|无任何跳变"; then
        NTR=$(echo "$OUT" | grep -oE "[0-9]+ 个跳变|跳变列表, [0-9]+" | head -1)
        printf "  %8s | %4s | ❌ 不运行   (%s)\n" "$MHZ" "$HCLK" "$NTR"
    else
        P=$(echo "$OUT" | grep "PA8 周期" | grep -oE "[0-9.]+ us")
        C=$(echo "$OUT" | grep "反推 CPU" | grep -oE "[0-9.]+ MHz" | head -1)
        D=$(echo "$OUT" | grep "偏差" | grep -oE "[-+][0-9.]+ %")
        printf "  %8s | %4s | ✅ 运行    拍周期 %s  反推 CPU %s  偏差 %s\n" \
               "$MHZ" "$HCLK" "$P" "$C" "$D"
    fi
done

echo
echo "说明: src/clock.h 已由 trap 自动还原 (见本脚本开头 H9 说明)。"
echo "      如需换回常用落点, 直接改 clock.h 的 CLK_PLL1_DIVN1 = 80u 后 bash build.sh。"
