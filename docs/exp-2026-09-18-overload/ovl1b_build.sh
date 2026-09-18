#!/usr/bin/env bash
# OVL-1b 步骤 1: 构建"受控超载档"
#   -DDCL_BOOT_SEL=0      扫描走 **FLASH 路径** (~3.6× 慢) = **成本模型预测不到的负载**
#   -DDCL_LOOP_RESET=0    **关掉主循环停滞自愈复位** —— 否则 ISR 一超载主循环就停,
#                         1.2s 后被 SYSRESETREQ 复位 ⇒ `ov` 被清零, 测不到
#   BOOT_PROFILE 保持 0   负载由我从协议侧逐档部署控制 (而不是靠内置 profile)
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9

echo "== 构建受控超载档 =="
bash build.sh -DDCL_BOOT_SEL=0 -DDCL_LOOP_RESET=0 > /tmp/ovl1b_build.log 2>&1
RC=$?; echo "BUILD_RC=$RC"
[ "$RC" -ne 0 ] && { tail -25 /tmp/ovl1b_build.log; exit "$RC"; }
grep -E "^DCL_(BOOT_SEL|BOOT_PROFILE|LOOP_RESET):" build/CMakeCache.txt
cp build/dcl_h723.hex .tmpctl/ovl1b.hex
md5sum .tmpctl/ovl1b.hex 2>/dev/null

echo "== 烧录 =="
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex 2>&1 | tail -2
echo "== reset =="
pyocd reset -t stm32h723xx 2>&1 | tail -2
sleep 12
echo "== 探活(空引擎, 应该活着) =="
python tools/h723_proto.py --port COM22 2>&1 | tail -3
