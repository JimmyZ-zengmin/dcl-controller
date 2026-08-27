# 测试MUX（多路选择器）
# MUX: 根据选择信号选择输出
# 语法: MUX name = src1 SELECT src2 ELSE src3
SENSOR temp FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR manual_mode FROM ADC1_CH1 SCALE 1.0 0.0
EQ is_manual FROM manual_mode == 1.0
MUX output = temp SELECT is_manual ELSE temp
OUTPUT output TO GPIO_PE0
