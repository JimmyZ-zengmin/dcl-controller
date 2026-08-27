# 测试ALARM（报警）
# ALARM: 当条件满足时触发报警
# 语法: ALARM name FROM src > value
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
ALARM overheat FROM temp > 80
OUTPUT overheat TO GPIO_PE0
