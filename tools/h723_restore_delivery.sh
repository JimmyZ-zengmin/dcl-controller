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
# ★★★ 2026-09-18（实测踩到）: **CMake 的选项是粘性缓存** —— `bash build.sh -DDCL_TICK_US=200`
#   之后, 再跑**无参**的 `build.sh` 仍然会构建 **200 µs 档**（缓存里存着 200）。
#   后果实测: 本脚本产出了 **200 µs 的"交付档"**, 而它照样打印 hex md5、照样探活 **12 PASS / 0 FAIL**
#     —— **探活判不出拍长**。是 `exp_eq_dt_semantics.py` 的 Q0a（tick 速率 vs 上位机墙钟）
#     把这件事抓出来的（它期望 10000 Hz, 实测 ~5000）★ 判据抓到了流程抓不到的东西。
#   ⇒ 两道修法:
#     ① 这里**显式传交付档的全部可粘性选项**（不靠默认值）;
#     ② 构建后**比指纹**（下面 EXPECT_MD5）⇒ 与基线不符就**大声失败**, 不烧。
DELIVERY_OPTS="-DDCL_TICK_US=100 -DDCL_BOOT_SEL=1 -DDCL_BOOT_SCAN_MODE=0 -DDCL_LOOP_RESET=1 -DDCL_STEP_RAMP_FIX=1"
EXPECT_MD5="c7c366c18232fcd36de12473d01f7329"     # ★ 100 µs 交付档基线（改部署期代码必然更新）
echo "  显式交付档选项: $DELIVERY_OPTS"
bash build.sh $DELIVERY_OPTS > /tmp/h723_restore_build.log 2>&1
RC=$?; echo "  BUILD_RC=$RC"
if [ "$RC" -ne 0 ]; then
    echo "  ❌ 构建失败 ⇒ **中止, 不要烧旧镜像**"
    tail -20 /tmp/h723_restore_build.log
    exit "$RC"
fi
grep -E "^DCL_(BOOT_SEL|BOOT_PROFILE|TICK_US|LOOP_RESET|STEP_RAMP_FIX):" build/CMakeCache.txt
GOT_MD5="$(md5sum build/dcl_h723.hex | cut -d' ' -f1)"
echo "  hex md5: $GOT_MD5"
if [ "$GOT_MD5" != "$EXPECT_MD5" ]; then
    echo "  ❌ **指纹与交付基线不符** ⇒ 这**不是**交付档（粘性选项? 源码改动?）"
    echo "     期望 $EXPECT_MD5"
    echo "     ⇒ **中止, 不烧**。确认无误后再更新本脚本的 EXPECT_MD5。"
    exit 3
fi
echo "  ✓ 指纹与交付基线一致"

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
