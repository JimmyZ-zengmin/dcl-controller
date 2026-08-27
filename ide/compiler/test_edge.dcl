# 测试EDGE（边沿检测）
# EDGE: 检测信号的上升沿或下降沿
# 语法: EDGE name FROM src RISING/FALLING
SENSOR btn FROM ADC1_CH0 SCALE 1.0 0.0
EDGE btn_pressed FROM btn RISING
OUTPUT btn_pressed TO GPIO_PE0
