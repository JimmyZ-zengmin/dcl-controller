#!/usr/bin/env bash
# build.sh — H723 工程一键构建
#
# 用法:  bash build.sh                 # 默认构建
#        bash build.sh clean           # 清掉 build/ (含 CMakeCache)
#        bash build.sh -DDCL_BOOT_GATE=1 -DDCL_BOOT_PROFILE=1
#
# 依赖 (本机已有, 无需安装):
#   cmake  4.0.3   C:/Espressif/tools/cmake/4.0.3/bin/cmake.exe
#   ninja  1.12.1  C:/Espressif/tools/ninja/1.12.1/ninja.exe
#   GCC    7.3.1   STM32CubeIDE 自带 (见 cmake/arm-none-eabi.cmake)
#
# ★ CMake 与 ninja 都是 Windows 程序: 必须传 Windows 风格路径 (正斜杠可以),
#   不能用 MSYS 的 /c/... 形式 —— 会报 "no such file or directory"。
#
# ★★ 纪律 (本项目的真事故): CMake 的 `-D` 是**缓存**的 —— 上一次
#   `-DDCL_DEPLOY_SELFTEST=1` 会被记住, 下一次不带该参数**不会**回到默认 0。
#   本项目真的因此把"自检版"当成交付固件烧进板子, 并据此读了一轮数据
#   (症状: nak_count=7 / n_routes=0, 而交付固件不该有这些)。且因为该构建
#   在 SWD 读回时看起来"一切正常", 极难察觉。
#   修法: 本脚本**每次显式传全部开关的默认值**, 再把用户参数追加在后面
#   (CMake 以最后一次 -D 为准) ⇒ 缓存永远被覆盖, 构建结果只由命令行决定。
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
# 转成 Windows 风格路径 (D:/...)
WIN_HERE="$(cd "$HERE" && pwd -W 2>/dev/null || echo "$HERE")"
BUILD="$WIN_HERE/build"

CMAKE="/c/Espressif/tools/cmake/4.0.3/bin/cmake.exe"
NINJA="C:/Espressif/tools/ninja/1.12.1/ninja.exe"

# ★★ Windows 下 GCC 的**中间文件** (.s/.o) 走 TMP/TEMP。若环境里 TEMP 指向
#   `C:\WINDOWS` (普通权限写不进去), 构建会以**与代码完全无关**的症状失败:
#       cc1.exe: fatal error: can't open 'C:\WINDOWS\ccXXXXXX.s' for writing: Permission denied
#   这个症状极易被误判成"我刚改坏了代码" (2026-09-11 真的踩到一次, 排查方向被带偏)。
#   ⇒ 显式把 TMP/TEMP 钉到工程 build/ 下: 构建结果不再依赖使用者环境的 TEMP 设置。
#      (与本项目"每次显式传全部开关默认值"同一条纪律: 别依赖外部状态的默认值。)
mkdir -p "$BUILD/tmp"
export TMP="$WIN_HERE/build/tmp"
export TEMP="$WIN_HERE/build/tmp"

if [ "$1" = "clean" ]; then
    rm -rf "$HERE/build"
    echo "cleaned: $HERE/build  (含 CMakeCache —— 下次构建回到全默认)"
    exit 0
fi

# ── 全部开关的默认值 (必须与 CMakeLists.txt 的 set(... CACHE ...) 默认值一致) ──
DEFAULTS=(
    -DDCL_BOOT_PROFILE=0
    -DDCL_BOOT_GATE=1
    -DDCL_BOOT_SEL=1
    -DDCL_BOOT_SCAN_MODE=0
    -DDCL_VTOR_ITCM=1
    -DSCAN_FLASH_PAD=0
    -DDCL_PA9_MODE=1
    -DDCL_MB_UART_ALT=1
    -DDCL_MB_BUILD_BUDGET=32
    -DDCL_MB_FAST_FRAME=1
    -DDCL_WDT=1
    -DDCL_WDT_MS=200
    -DDCL_WDT_FEED_GATE=1
    -DDCL_WDT_SYNC_CYC=40000000
    -DDCL_WDT_START_FIRST=1
    -DDCL_WDT_PR=4
    -DDCL_WDT_PERSIST_WINDOW=8000
    -DDCL_LOOP_RESET=1
    -DDCL_PERSIST_SAVE=0
    -DDCL_BOOT_BANNER=1
    -DDCL_BANNER_PERIOD=0
    -DDCL_UART_SELFTEST=0
    -DDCL_UART_BAUD=115200
    -DDCL_DEPLOY_SELFTEST=0
    -DDCL_MIN_UART=0
    -DDCL_MIN_UART2=0
    -DDCL_HIL_SAFE=1
    -DDCL_IO_IN_ISR=1
)

"$CMAKE" -S "$WIN_HERE" -B "$BUILD" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$WIN_HERE/cmake/arm-none-eabi.cmake" \
    -DCMAKE_MAKE_PROGRAM="$NINJA" \
    "${DEFAULTS[@]}" "$@"

# ── 构建 (同时落日志, 供下面的"零警告闸门"检查) ──
# ★★ 这是一道**硬闸门**, 起因是真事故: uart.c 的 `NVIC_ISER = (1u << 37)` 位移溢出,
#    GCC 报了 -Wshift-count-overflow, 但同次构建还有 24 条无害警告 (newlib 桩的
#    unused-parameter 等) 把真警告淹没 → 我们只 grep 了 error, 于是带病固件上线,
#    USART1 中断从未使能、协议层整轮不可用, 而所有"配置类"寄存器检查全绿。
#    两道防线: ① CMakeLists 里 -Werror (警告即编译失败)
#              ② 这里再扫一遍完整日志 (含链接器/汇编的警告)
set -o pipefail
LOG="$HERE/build/buildlog.txt"
# ★★ 已知间歇性失败 (2026-09-12 一天踩 3 次, 且**不一定重跑一次就收敛**):
#     cc1.exe: fatal error: can't open '...\build\tmp\ccXXXXXX.s' for writing: Permission denied
#   随机文件、随机名、且失败的文件里包含**从未改动过**的 .c ⇒ 与代码无关 (已知族)。
#   ⇒ 这里加**一次自动重试**; 但**显式打印出来, 不静默** ——
#     静默重试会把"真的编译失败"掩盖成"重试后还是失败", 丢掉第一次的错误信息。
if ! "$CMAKE" --build "$BUILD" 2>&1 | tee "$LOG"; then
    echo
    if grep -q "Permission denied" "$LOG" && grep -q "can't open" "$LOG"; then
        echo "★★ 命中已知症状 (build/tmp Permission denied, 与代码无关) ⇒ 清 tmp 后自动重试一次。"
        rm -rf "$BUILD/tmp"
        mkdir -p "$BUILD/tmp"
        "$CMAKE" --build "$BUILD" 2>&1 | tee "$LOG"
    else
        echo "★★ 构建失败, 且**不是**已知的 build/tmp 症状 ⇒ 按真实构建失败处理 (见上面的日志)。"
        exit 1
    fi
fi

NW=$(grep -c "warning:" "$LOG" || true)
if [ "$NW" != "0" ]; then
    echo
    echo "★★ 构建产生 $NW 条警告 —— 拒绝通过。"
    echo "   本项目不接受'无害警告': 噪声会把真警告埋掉 (A1 事故就是这么发生的)。"
    echo "   要么修掉, 要么对该文件显式关掉**那一种**警告并写明理由。"
    echo "── 警告清单 ──"
    grep "warning:" "$LOG"
    exit 1
fi
echo "✓ 零警告 (本工程源文件 + 链接器 + 汇编)"

# ── ★★ ISR 调用树闸门 (2026-09-13 正式接入) ──────────────────────────────
# ★ 为什么必须有这一步: `src/itcm.h` 早就**宣称**过"构建期的 ISR 调用树闸门会把
#   '忘了加 DCL_ITCM'变成'构建不过'" —— 但脚本写好后**从未接进构建**。
#   宣称落空的代价就是本次事故: `hil_out_apply` 漏在 flash ⇒ 擦 flash 时拍 ISR
#   取指被 stall ⇒ ISR 不返回 ⇒ 喂狗停 ⇒ 看门狗复位 ⇒ "保存配置"变成"重启机器",
#   而且**配置从未落盘**。这条路径活过了 4 轮外部审计。
#   ★ 不变量本身见 src/itcm.h; 判据与 6 个已修仪器 bug 见 tools/gate_isr_itcm.py。
# ★ 找不到 python 时**显式警告并跳过**, 不静默 (静默跳过 = 闸门有洞, 本项目最恨的形态)。
PY_BIN="${DCL_PYTHON:-$(command -v python || command -v python3 || true)}"
if [ -z "$PY_BIN" ]; then
    echo
    echo "⚠️ 未找到 python ⇒ **跳过** ISR 调用树闸门 —— src/itcm.h 的不变量本次未被校验。"
    echo "   恢复方式: 装 python, 或设 DCL_PYTHON=<解释器绝对路径>。"
else
    echo
    echo "── ISR 调用树闸门 (src/itcm.h 的不变量: ISR 可达 ⇒ 必须住 ITCM) ──"
    # ★ 必须用 WIN_HERE (Windows 风格): python.exe 是 Windows 程序, 传 MSYS 风格
    #   的 `/d/STM/...` 会被它当成"相对当前盘符"⇒ 报 `D:\d\STM\...` 找不到文件。
    if ! "$PY_BIN" "$WIN_HERE/tools/gate_isr_itcm.py" "$WIN_HERE/build/dcl_h723"; then
        echo
        echo "★★ ISR 调用树闸门失败 ⇒ 拒绝通过。"
        echo "   上表列出的函数**擦 flash 时会让拍 ISR 卡死**(取指/读常量被 stall)。"
        echo "   修法: 给它们加 DCL_ITCM (见 src/itcm.h); 只读常量表用 .itcm_rodata 段。"
        exit 1
    fi
fi

# ── 打印**实际生效**的开关 (不是"我以为传了什么") ──
echo
echo "── 生效开关 (读自 CMakeCache) ──"
# ★ 正则必须含数字: 原来写 `^DCL_[A-Z_]*:` —— `DCL_MIN_UART2` 因为结尾是 2 而**不在
#   这个清单里**, 也就是说"看上去列全了, 其实漏了一个"。这一段的唯一目的就是
#   "别被缓存骗", 漏列一个开关等于给它留了一个静默洞。⇒ 改成 [A-Z0-9_]*。
grep -E "^(DCL_[A-Z0-9_]*|SCAN_FLASH_PAD):(STRING|BOOL)=" "$HERE/build/CMakeCache.txt" \
    | sed 's/:[A-Z]*=/ = /' | sed 's/^/   /'
echo
echo "产物: $HERE/build/dcl_h723 (ELF, 无扩展名) + .bin + .hex + .map"
echo "烧录: pyocd flash -t stm32h723xx -O connect_mode=under-reset \"$HERE/build/dcl_h723.hex\""
