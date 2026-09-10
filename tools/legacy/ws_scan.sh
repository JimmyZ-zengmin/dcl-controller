#!/usr/bin/env bash
# ws_scan.sh — 扫 FLASH 等待态 vs 频率 (固件法, 可靠)
# 用法: bash ws_scan.sh <WS> <N1> [N2 ...]     N = MHz/5
set -u
WS="$1"; shift
CMAKE="/c/Espressif/tools/cmake/4.0.3/bin/cmake.exe"
NM="/c/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe"
cd "$(dirname "$0")"

for N in "$@"; do
  MHZ=$(( N * 5 ))
  python - "$N" "$WS" <<'PYEOF'
import sys, re
n, w = sys.argv[1], sys.argv[2]
f='src/clock.h'
s=open(f,encoding='utf-8').read()
s=re.sub(r'#define CLK_PLL1_DIVN1\s+\d+u', '#define CLK_PLL1_DIVN1  %su' % n, s)
s=re.sub(r'#define CLK_FLASH_WS\s+\d+u', '#define CLK_FLASH_WS    %su' % w, s)
open(f,'w',encoding='utf-8').write(s)
PYEOF

  "$CMAKE" --build build >/dev/null 2>&1 || { echo "  ${MHZ}MHz WS=$WS 编译失败"; continue; }

  A=$("$NM" build/dcl_h723 | grep -E " g_boot_status| g_clock_hclk| g_stage| g_tick_count" | awk '{print "0x"$1}' | tr '\n' ' ')
  set -- $A

  pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex >/dev/null 2>&1
  pyocd cmd -t stm32h723xx -O connect_mode=under-reset -c "reset" -c "sleep 2500" >/dev/null 2>&1
  OUT=$(pyocd cmd -t stm32h723xx -O connect_mode=under-reset -c "reset halt" \
        -c "read32 $1" -c "read32 $2" -c "read32 $3" -c "read32 $4" 2>&1 \
        | grep -E "^[0-9a-f]{8}:" | awk '{print $2}')
  B=$(echo "$OUT" | sed -n 1p); H=$(echo "$OUT" | sed -n 2p)
  S=$(echo "$OUT" | sed -n 3p); T=$(echo "$OUT" | sed -n 4p)
  [ "$T" = "00000000" ] && V="X 跑不住" || V="O 跑通"
  printf "  %4dMHz WS=%-2s | boot=%s hclk=%s stage=%s tick=%s | %s\n" "$MHZ" "$WS" "$B" "$H" "$S" "$T" "$V"
done
