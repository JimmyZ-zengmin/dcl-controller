# 测试CONST（常量）
# CONST: 声明一个常量
CONST setpoint = 60.0
CONST kp = 2.0
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
ARITH error = setpoint SUB temp
ARITH output = error MUL kp
OUTPUT output TO GPIO_PE0
