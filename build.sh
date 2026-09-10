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

if [ "$1" = "clean" ]; then
    rm -rf "$HERE/build"
    echo "cleaned: $HERE/build  (含 CMakeCache —— 下次构建回到全默认)"
    exit 0
fi

# ── 全部开关的默认值 (必须与 CMakeLists.txt 的 set(... CACHE ...) 默认值一致) ──
DEFAULTS=(
    -DDCL_BOOT_PROFILE=0
    -DDCL_BOOT_GATE=0
    -DDCL_BOOT_SEL=1
    -DDCL_BOOT_SCAN_MODE=0
    -DSCAN_FLASH_PAD=0
    -DDCL_PA9_MODE=1
    -DDCL_BOOT_BANNER=1
    -DDCL_BANNER_PERIOD=0
    -DDCL_UART_SELFTEST=0
    -DDCL_UART_BAUD=115200
    -DDCL_DEPLOY_SELFTEST=0
)

"$CMAKE" -S "$WIN_HERE" -B "$BUILD" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$WIN_HERE/cmake/arm-none-eabi.cmake" \
    -DCMAKE_MAKE_PROGRAM="$NINJA" \
    "${DEFAULTS[@]}" "$@"

"$CMAKE" --build "$BUILD"

# ── 打印**实际生效**的开关 (不是"我以为传了什么") ──
echo
echo "── 生效开关 (读自 CMakeCache) ──"
grep -E "^(DCL_[A-Z_]*|SCAN_FLASH_PAD):(STRING|BOOL)=" "$HERE/build/CMakeCache.txt" \
    | sed 's/:[A-Z]*=/ = /' | sed 's/^/   /'
echo
echo "产物: $HERE/build/dcl_h723 (ELF, 无扩展名) + .bin + .hex + .map"
echo "烧录: pyocd flash -t stm32h723xx -O connect_mode=under-reset \"$HERE/build/dcl_h723.hex\""
