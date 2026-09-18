#!/usr/bin/env bash
# 两档构建 + 校验（扫描路径成本模型的两侧绊线断言都必须在各自档位上成立）
#
# 为什么必须有这个脚本:
#   `src/engine.h` 的绊线断言是**两侧**的, 而两侧只有在**各自的 BOOT_SEL** 下才被编译:
#     交付档 (BOOT_SEL=1): 128 × 145 = 18560 ≤ 26000 ⇒ 预算门**不应**具约束力
#     FLASH 档(BOOT_SEL=0): 128 × 432 = 55296 >  26000 ⇒ 预算门**应当**具约束力
#   ⇒ **只构建交付档 = 只验了一半**。本脚本一次把两档都构出来, 让两条断言都被求值。
#
# ★ 用法: bash tools/h723_build_variants.sh
# ★ 只构建, **不烧录**（烧录与恢复用 tools/h723_restore_delivery.sh）
# ★ 本脚本自带 PATH 导出 —— 见 RULES-DETAIL §5.38（本机 bash 起来时 PATH 为空,
#   不导出会出现 `dirname: command not found` 这种看似"脚本坏了"的症状）。
export PATH="/usr/bin:/bin:/mingw64/bin:/c/Windows/System32:$PATH"
cd "$(dirname "$0")/.." || exit 9

RC_ALL=0

echo "════ A) 交付档 (BOOT_SEL=1) —— 期望 RC=0 ════"
# ★★ 显式传"可粘性"选项: CMake 的 `-D` 会**留在缓存里**, 无参 build.sh 会沿用上一次的值
#   ⇒ 实测踩过: `-DDCL_TICK_US=200` 之后这里产出的"交付档"其实是 **200 µs 档**
#     （见 RETRACTIONS P30 / docs/exp-EU-tick-and-cost.md §④）。
bash build.sh -DDCL_TICK_US=100 -DDCL_BOOT_SEL=1 -DDCL_LOOP_RESET=1 > /tmp/h723_variants_delivery.log 2>&1
RC=$?; echo "  DELIVERY_RC=$RC"
if [ "$RC" -ne 0 ]; then
    echo "  ❌ 交付档构建失败 —— **不要往下走, 也不要烧旧镜像**"
    grep -iE "error:|Static_assert|断言" /tmp/h723_variants_delivery.log | head -6
    tail -6 /tmp/h723_variants_delivery.log
    RC_ALL=1
else
    grep -E "^DCL_BOOT_(SEL|PROFILE):" build/CMakeCache.txt
    echo "  hex md5: $(md5sum build/dcl_h723.hex | cut -d' ' -f1)"
    echo "  警告数: $(grep -ci warning /tmp/h723_variants_delivery.log)"
    echo "  ★ 历史基线 (2026-09-18 路径成本改动前后的**逐字节**对照值):"
    echo "      80c05c0eb8f00ade3ba5b8985b711826  ← 两者曾相同 ⇒ 该改动对交付档零回归"
    echo "  ★★ 当前基线 (2026-09-18 晚, 修 2 个缺陷 + 加 3 个观测域之后):"
    echo "      40f24cf199abb8dd6b888740aa39e611  ← **现在的交付档指纹**"
    echo "      (历史值 80c05c0e… / 6af8ca28… 均作废; 原因见 .workbuddy/memory/MEMORY.md)"
    echo "      ⇒ 上面那行 hex md5 与这一行**相同**才算交付档无回归。"
fi

echo
echo "════ B) FLASH 档 (BOOT_SEL=0 + LOOP_RESET=0) —— 期望 RC=0 ════"
# ★ LOOP_RESET=0 是**实验纪律**: ISR 一旦长期超拍, 主循环会饿死并撞 LOOP_RESET ⇒
#   复位循环, 而复位会清零 ov/samples ⇒ **自己把自己的观测毁掉**（见保障图谱 §3）。
bash build.sh -DDCL_BOOT_SEL=0 -DDCL_LOOP_RESET=0 > /tmp/h723_variants_flash.log 2>&1
RC=$?; echo "  FLASH_RC=$RC"
if [ "$RC" -ne 0 ]; then
    echo "  ❌ FLASH 档构建失败（若报 Static_assert ⇒ 路径成本表/门值/最贵原语常量变了, 需重新评估）"
    grep -iE "error:|Static_assert|断言" /tmp/h723_variants_flash.log | head -6
    RC_ALL=1
else
    grep -E "^DCL_(BOOT_SEL|LOOP_RESET):" build/CMakeCache.txt
    echo "  hex md5: $(md5sum build/dcl_h723.hex | cut -d' ' -f1)"
    echo "  警告数: $(grep -ci warning /tmp/h723_variants_flash.log)"
fi

echo
echo "★ 注意: 两档构建都会覆盖 build/, 当前 build/ 是 **B) FLASH 档**。"
echo "  要把板子恢复成交付档: bash tools/h723_restore_delivery.sh"
exit "$RC_ALL"
