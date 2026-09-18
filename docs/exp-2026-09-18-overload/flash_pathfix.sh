#!/usr/bin/env bash
# 烧路径价修复后的 FLASH 档 (build/ 里现在就是它)
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex 2>&1 | tail -2
pyocd reset -t stm32h723xx 2>&1 | tail -1
sleep 12
python tools/h723_proto.py --port COM22 2>&1 | tail -3
