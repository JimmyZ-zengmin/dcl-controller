# 测试新增6种原语 (LIMIT/MAX/MIN/ABS/EQ/NE)
# 输入：ADC1_CH0 → SENSOR[0]

# 信号调理
SENSOR  temp     FROM ADC1_CH0    SCALE 1.0 0.0

# 测试新原语
LIMIT   clamped  FROM temp        RANGE -10 10      # 限幅到[-10, +10]
MAX     max_val  = temp MAX 5.0                     # 取max(temp, 5.0)
MIN     min_val  = temp MIN 3.0                     # 取min(temp, 3.0)
ABS     abs_val  FROM temp                          # 绝对值

# 比较测试
EQ      is_five  FROM temp == 5.0                   # 等于5.0
NE      not_five FROM temp != 5.0                   # 不等于5.0

# 输出到WIRE（便于pyocd读取验证）
OUTPUT  clamped  TO GPIO_PE0
OUTPUT  max_val  TO GPIO_PE1
OUTPUT  min_val  TO GPIO_PE2
OUTPUT  abs_val  TO GPIO_PE3
OUTPUT  is_five  TO GPIO_PE4
OUTPUT  not_five TO GPIO_PE5
