# 最小测试: 传感器 → 直通 → TON定时器
SENSOR  temp     FROM ADC1_CH0
TIMER   t1: IN=temp, PT=500ms → Q=delayed_out, ET=elapsed
