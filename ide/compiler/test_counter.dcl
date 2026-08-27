# 测试COUNTER（计数器）
# COUNTER: 计数脉冲
# 语法: COUNTER name: CU=input, PV=preset → Q=output, CV=count
SENSOR pulse FROM ADC1_CH0 SCALE 1.0 0.0
COUNTER c1: CU=pulse, PV=100 → Q=full, CV=count
OUTPUT full TO GPIO_PE0
OUTPUT count TO GPIO_PE1
