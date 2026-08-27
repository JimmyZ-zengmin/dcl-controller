# 测试BIT（位运算）
# BIT: 位操作（AND/OR/XOR/NOT）
# 语法: BIT name = src1 OP src2 或 BIT name = NOT src
SENSOR flags FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR mask FROM ADC1_CH1 SCALE 1.0 0.0
BIT masked = flags BITAND mask
BIT inverted = BITNOT flags
OUTPUT masked TO GPIO_PE0
OUTPUT inverted TO GPIO_PE1
