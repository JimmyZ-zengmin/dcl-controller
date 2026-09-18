#!/usr/bin/env bash
# OVL-1 步骤 2: 烧录超载档 + 复位让板子自己跑
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9

echo "== flash OVERLOAD (BOOT_SEL=0 / BOOT_PROFILE=2) =="
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex 2>&1 | tail -6
echo "FLASH_RC=${PIPESTATUS[0]}"

echo "== reset (让板子自己重启; 时基是 TIM5, 不受 pyocd 停 DWT 影响) =="
pyocd reset -t stm32h723xx 2>&1 | tail -3
echo "RESET_RC=$?"
sleep 3
echo "== 复位后先探活 =="
python tools/h723_proto.py --port COM22 2>&1 | tail -4
