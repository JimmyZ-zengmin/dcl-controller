#!/usr/bin/env bash
# 把板子恢复成交付档 —— **实验纪律**: 不许把实验档留在板子上
#
# 为什么单独一个脚本: 实验档（`BOOT_SEL=0` 等）会让板子的行为与交付**不同**,
# 而"板子上现在是哪一档"没有任何协议手段能读出来 ⇒ 只能靠**流程**保证。
# 本脚本把这个流程固化: 重建默认档 → 烧回 → 复位 → 等自愈窗 → 探活。
#
# ★ 用法: bash tools/h723_restore_delivery.sh
# ★ 探活用 tools/h723_proto.py（期望 **12 PASS / 0 FAIL**）
# ★ 自带 PATH 导出 —— 见 RULES-DETAIL §5.38
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9
export DCL_PORT="${DCL_PORT:-COM22}"

echo "== 1) 重建默认档 =="
bash build.sh > /tmp/h723_restore_build.log 2>&1
RC=$?; echo "  BUILD_RC=$RC"
if [ "$RC" -ne 0 ]; then
    echo "  ❌ 构建失败 ⇒ **中止, 不要烧旧镜像**"
    tail -20 /tmp/h723_restore_build.log
    exit "$RC"
fi
grep -E "^DCL_BOOT_(SEL|PROFILE):" build/CMakeCache.txt
echo "  hex md5: $(md5sum build/dcl_h723.hex | cut -d' ' -f1)"

echo
echo "== 2) 烧回 =="
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex 2>&1 | tail -3
echo
echo "== 3) 复位 + 等自愈窗 (~12 s) =="
pyocd reset -t stm32h723xx 2>&1 | tail -2
sleep 12
echo
echo "== 4) 探活 (期望 12 PASS / 0 FAIL) =="
python tools/h723_proto.py --port "$DCL_PORT" 2>&1 | tail -5
