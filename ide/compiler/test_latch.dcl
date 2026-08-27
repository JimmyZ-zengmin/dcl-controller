# 测试LATCH（RS触发器/锁存器）
# LATCH: Set-Reset 锁存器
# 语法: LATCH name: S1=set, R=reset → Q1=output
SENSOR set_btn FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR reset_btn FROM ADC1_CH1 SCALE 1.0 0.0
LATCH sr1: S1=set_btn, R=reset_btn → Q1=latch_out
OUTPUT latch_out TO GPIO_PE0
