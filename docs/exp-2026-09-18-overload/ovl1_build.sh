#!/usr/bin/env bash
# OVL-1 步骤 1: 存交付固件 + 构建"超载档" (FLASH 扫描路径 = 模型外负载)
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9
mkdir -p .tmpctl

echo "== 0) 存当前(交付)固件 =="
cp build/dcl_h723.hex .tmpctl/delivery.hex
md5sum .tmpctl/delivery.hex 2>/dev/null || certutil -hashfile .tmpctl/delivery.hex MD5 | head -2

echo
echo "== 1) 构建超载档: -DDCL_BOOT_SEL=0 (FLASH 扫描) -DDCL_BOOT_PROFILE=2 =="
bash build.sh -DDCL_BOOT_SEL=0 -DDCL_BOOT_PROFILE=2 > /tmp/ovl1_build.log 2>&1
RC=$?
echo "BUILD_RC=$RC"
if [ "$RC" -ne 0 ]; then
  echo "!!! 构建失败 ⇒ 按纪律**不许往下走**（不烧旧镜像）"
  tail -25 /tmp/ovl1_build.log
  exit "$RC"
fi
cp build/dcl_h723.hex .tmpctl/overload.hex
echo "overload.hex:"; md5sum .tmpctl/overload.hex 2>/dev/null
echo "--- 生效开关(关键两项) ---"
grep -E "^DCL_BOOT_(SEL|PROFILE):" build/CMakeCache.txt
echo "--- 构建尾部 ---"
tail -5 /tmp/ovl1_build.log
