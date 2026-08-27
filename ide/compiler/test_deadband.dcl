# 测试DEADBAND（死区）
# DEADBAND: 在指定范围内输出0，超出范围输出实际值
# 语法: DEADBAND name FROM src RANGE lo hi
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
DEADBAND temp_db FROM temp RANGE -5 5
OUTPUT temp_db TO GPIO_PE0
