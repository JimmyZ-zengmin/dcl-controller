# 测试TIMER（定时器）
# TIMER: 延时定时器
# 语法: TIMER name: IN=input, PT=time → Q=output
SENSOR btn FROM ADC1_CH0 SCALE 1.0 0.0
TIMER t1: IN=btn, PT=3s → Q=motor_on
OUTPUT motor_on TO GPIO_PE0
