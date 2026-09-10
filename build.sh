#!/usr/bin/env bash
# build.sh — H723 工程一键构建
#
# 用法:  bash h723/build.sh          # 构建
#        bash h723/build.sh clean    # 清理
#
# 依赖 (本机已有, 无需安装):
#   cmake  4.0.3   C:/Espressif/tools/cmake/4.0.3/bin/cmake.exe
#   ninja  1.12.1  C:/Espressif/tools/ninja/1.12.1/ninja.exe
#   GCC    7.3.1   STM32CubeIDE 自带 (见 cmake/arm-none-eabi.cmake)
#
# ★ CMake 与 ninja 都是 Windows 程序: 必须传 Windows 风格路径 (正斜杠可以),
#   不能用 MSYS 的 /c/... 形式 —— 会报 "no such file or directory"。
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
# 转成 Windows 风格路径 (D:/...)
WIN_HERE="$(cd "$HERE" && pwd -W 2>/dev/null || echo "$HERE")"
BUILD="$WIN_HERE/build"

CMAKE="/c/Espressif/tools/cmake/4.0.3/bin/cmake.exe"
NINJA="C:/Espressif/tools/ninja/1.12.1/ninja.exe"

if [ "$1" = "clean" ]; then
    rm -rf "$HERE/build"
    echo "cleaned: $HERE/build"
    exit 0
fi

"$CMAKE" -S "$WIN_HERE" -B "$BUILD" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$WIN_HERE/cmake/arm-none-eabi.cmake" \
    -DCMAKE_MAKE_PROGRAM="$NINJA"

"$CMAKE" --build "$BUILD"

echo
echo "产物: $HERE/build/dcl_h723.{elf,bin,hex}"
echo "烧录: pyocd flash -t stm32h723xx \"$HERE/build/dcl_h723.hex\""
