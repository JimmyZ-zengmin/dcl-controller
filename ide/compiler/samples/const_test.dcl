# 最简单的测试程序: CONST(1.0) → ACT[32] → GPIO bit 0
# 1 条路由,无计算抖动来源

SENSOR dummy FROM ADC1_CH0    SCALE 1.0 0.0

# 直接把 CONST(1.0) 写到 ACT[32]
CONST one = 1.0
