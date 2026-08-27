# 测试RATE（变化率限制）
# RATE: 限制信号的变化速率
# 语法: RATE name FROM src MAX rate
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
RATE temp_rated FROM temp MAX 10.0
OUTPUT temp_rated TO GPIO_PE0
