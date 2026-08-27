# 测试FILTER（低通滤波）
# FILTER: 对信号进行低通滤波，a为滤波系数（0-1，越小滤波越强）
SENSOR temp FROM ADC1_CH0 SCALE 1.0 0.0
FILTER temp_f FROM temp LOWPASS a=0.1
OUTPUT temp_f TO GPIO_PE0
