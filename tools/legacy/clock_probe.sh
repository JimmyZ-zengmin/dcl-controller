#!/usr/bin/env bash
# ============================================================================
# clock_probe.sh — H723 时钟树 SWD 探测 (不依赖固件, 纯探针配置)
#
# 用途: 在不能烧录/固件挂掉时, 用调试器直接配置时钟并读回验证。
#       这是"排雷"的标准工具 —— 每一步都有读回, 失败点一目了然。
#
# 用法:
#   bash clock_probe.sh <CPU_MHz> [vesc_mode]
#     CPU_MHz   : 25 的整数倍, 且在 100..550 内 (VCO = CPU, DIVP1 = 1)
#     vosc_mode : direct (默认, 直接写 VOS0) | stepped (逐级 2→1→0)
#
# ★关键知识 (2026-09-10 实测 + 文献):
#   1. PLLCFGR.DIVP1EN (bit16) 必须置 1, 否则 SYSCLK 切换被静默拒绝
#      (SWS 永不跟随) —— 本次排掉的最大一颗雷。
#   2. PLLCFGR 写入必须在 PLL1ON=0 时进行。
#   3. ★VOS 必须**逐级**切换: Scale3→Scale2→Scale1→Scale0, 不能跳级
#      (ST HAL 文档: "Transition to Voltage Scale 0 is only possible when
#       the system is already in Voltage Scale 1")
#      实测症状: 从复位档直接写 VOS0 → D3CR 读回 0b00 但**实际电压没升**,
#      表现是超过 300MHz (Scale2 上限) 后 AXI 读回全 0。
#   4. VOS 编码 (RM0468 §6.8.6, libopencm3 标注 H72x/3x):
#      0b00=Scale0(550M) 0b01=Scale3 0b10=Scale2(300M) 0b11=Scale1(400M)
#   5. ★pyocd 的 `reset` 命令**不复位 RCC**; connect_mode=under-reset 会
#      在**每次连接**时拉低 NRST → 多步实验必须在**单次连接**内完成。
#   6. under-reset 是锁死芯片 (SWD 全部 AP 失联) 的唯一恢复手段。
#
# 前置: 探针已连接 (pyocd list 能看到); 内核为 stm32h723xx
# ============================================================================
set -u

MHZ="${1:-520}"
MODE="${2:-stepped}"          # 默认逐级 (安全, 符合 ST 要求)

if [ "$MHZ" = "--help" ] || [ -z "$MHZ" ]; then
    sed -n '3,30p' "$0"; exit 0
fi

N=$(( MHZ / 5 ))              # VCO = 5MHz × N  → N = MHz/5
DIVR=$(printf "0x%08X" $(( (N-1) | 0x30000 | 0x1800000 )))

# VOS 寄存器 (PWR_D3CR) 与各级值
D3CR=0x58024818
# VOS 编码 (RM0468): 0=Scale0 / 1=Scale3 / 2=Scale2 / 3=Scale1
VOS_S2=$((2 << 14))           # Scale2 = 0x8000
VOS_S1=$((3 << 14))           # Scale1 = 0xC000
VOS_S0=$((0 << 14))           # Scale0 = 0x0000

P="pyocd cmd -t stm32h723xx -O connect_mode=under-reset"

echo "═══ H723 时钟探测: ${MHZ}MHz  (VOS 模式: ${MODE}) ═══"
echo "   PLL1DIVR = ${DIVR}  (DIVN1=${N})"

# 组装单次连接的命令序列
ARGS=(
  -c "reset halt"
  -c "read32 0x58024818"                      # 复位时的 VOS 档

  # ---- VOS 逐级升压 (RTEMS/HAL 文档要求) ----
  -c "write32 $D3CR 0x$(printf '%08X' $VOS_S2)" -c "sleep 30"
  -c "read32 $D3CR"
)
if [ "$MODE" = "stepped" ]; then
  ARGS+=(
    -c "write32 $D3CR 0x$(printf '%08X' $VOS_S1)" -c "sleep 30"
    -c "read32 $D3CR"
  )
fi
ARGS+=(
  -c "write32 $D3CR 0x$(printf '%08X' $VOS_S0)" -c "sleep 50"
  -c "read32 $D3CR"                           # ★Scale0 回读 (0x2000 = VOS0+VOSRDY)

  # ---- FLASH 等待态 (VOS0 @ AXI≤275MHz → LATENCY=3, WRHIGHFREQ=3) ----
  -c "write32 0x52002000 0x00000033"

  # ---- HSE ----
  -c "write32 0x58024400 0x00014025"
  -c "sleep 300"
  -c "read32 0x58024400"                      # 0x00034025 = HSERDY ✓

  # ---- PLL1 ----
  -c "write32 0x58024428 0x00000052"          # DIVM1=5, PLLSRC=HSE
  -c "write32 0x5802442C 0x00010008"          # RGE=2 | ★DIVP1EN(bit16)
  -c "write32 0x58024430 $DIVR"
  -c "read32 0x5802442C"                      # 确认 DIVP1EN 落盘
  -c "write32 0x58024400 0x01014025"          # PLL1ON
  -c "sleep 300"
  -c "read32 0x58024400"                      # bit25 PLL1RDY

  # ---- 切 SYSCLK ----
  -c "write32 0x58024410 0x00000003"
  -c "sleep 200"
  -c "read32 0x58024410"                      # ★0x1B = SW3/SWS3 成功
  -c "read32 0xE000ED00"                      # CPUID (0x411FC272 = 存活)
)

OUT=$($P "${ARGS[@]}" 2>&1)
echo "$OUT" | grep -E "^[0-9a-f]{8}:|Error|no selected core" | nl -ba

echo
echo "判读:"
echo "  1 = 复位 VOS 档      (0x6000 = VOS=01)"
echo "  2 = Scale2 回读"
[ "$MODE" = "stepped" ] && echo "  3 = Scale1 回读"
echo "  末3 = PLLCFGR / RCC_CR / RCC_CFGR / CPUID"
echo "  ★RCC_CFGR 末字节 1b = 切换成功; 00 + CPUID 0 = AXI 失效(电压不足)"
