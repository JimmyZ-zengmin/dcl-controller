#!/usr/bin/env bash
# 恢复交付固件 (默认档) —— 实验纪律: 不许把实验档留在板子上
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9

echo "== 1) 重建默认档 =="
bash build.sh > /tmp/restore_build.log 2>&1
RC=$?; echo "BUILD_RC=$RC"
[ "$RC" -ne 0 ] && { tail -20 /tmp/restore_build.log; exit "$RC"; }
grep -E "^DCL_BOOT_(SEL|PROFILE):" build/CMakeCache.txt
cp build/dcl_h723.hex .tmpctl/restored.hex
echo "--- md5 对比 (应与 delivery.hex 相同) ---"
md5sum .tmpctl/delivery.hex .tmpctl/restored.hex 2>/dev/null

echo
echo "== 2) 烧回 =="
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex 2>&1 | tail -3
echo "== 3) reset =="
pyocd reset -t stm32h723xx 2>&1 | tail -2
sleep 12
echo "== 4) 探活 =="
python tools/h723_proto.py --port COM22 2>&1 | tail -5
