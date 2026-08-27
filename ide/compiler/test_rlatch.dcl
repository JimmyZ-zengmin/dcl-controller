# 测试RLATCH（复位优先触发器）
# RLATCH: Reset-dominant Latch
# 语法: RLATCH name: S=set, R1=reset → Q1=output
SENSOR start FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR estop FROM ADC1_CH1 SCALE 1.0 0.0
RLATCH safe: S=start, R1=estop → Q1=active
OUTPUT active TO GPIO_PE0
