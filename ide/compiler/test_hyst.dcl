# 测试HYST（滞回比较器）
# HYST: 带滞回的比较器，防止在阈值附近抖动
# 语法: HYST name FROM src HIGH hi LOW lo
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
HYST heater_on FROM temp HIGH 80 LOW 75
OUTPUT heater_on TO GPIO_PE0
