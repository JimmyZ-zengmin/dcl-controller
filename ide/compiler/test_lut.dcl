# 测试LUT（查表）
# LUT: 一维查表，将输入映射为输出
# 语法: LUT name FROM src TABLE v1 v2 v3 v4 v5
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
LUT curve FROM temp TABLE 0.0 0.5 1.0 0.8 0.3
OUTPUT curve TO GPIO_PE0
