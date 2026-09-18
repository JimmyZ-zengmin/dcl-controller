#!/usr/bin/env bash
# 路径成本价修复: 两档构建
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9

echo "════ A) 交付档 (BOOT_SEL=1) —— 期望 RC=0 且行为不变 ════"
bash build.sh > /tmp/pf_delivery.log 2>&1; echo "  DELIVERY_RC=$?"
grep -E "^DCL_BOOT_SEL:" build/CMakeCache.txt
cp build/dcl_h723.hex .tmpctl/pathfix_delivery.hex
md5sum .tmpctl/pathfix_delivery.hex 2>/dev/null
echo "  警告数: $(grep -ci warning /tmp/pf_delivery.log)"
grep -iE "error:|断言|Static_assert" /tmp/pf_delivery.log | head -4

echo
echo "════ B) FLASH 档 (BOOT_SEL=0 + LOOP_RESET=0) —— 旧断言下这个构建会编不过 ════"
bash build.sh -DDCL_BOOT_SEL=0 -DDCL_LOOP_RESET=0 > /tmp/pf_flash.log 2>&1; echo "  FLASH_RC=$?"
grep -E "^DCL_(BOOT_SEL|LOOP_RESET):" build/CMakeCache.txt
cp build/dcl_h723.hex .tmpctl/pathfix_flash.hex
md5sum .tmpctl/pathfix_flash.hex 2>/dev/null
echo "  警告数: $(grep -ci warning /tmp/pf_flash.log)"
echo "  --- 尾部 ---"; tail -4 /tmp/pf_flash.log
