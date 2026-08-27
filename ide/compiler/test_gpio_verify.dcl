# GPIO 输出验证测试程序
# 目的: 验证 DCL 编译器 + 部署 + GPIO 输出正确性
# 复杂度: 中等 (8 路由, 包含传感器、逻辑、定时器、GPIO 输出)

# ============================================
# 1. 常量声明
# ============================================
CONST threshold = 0.5
CONST temp_sp = 25.0

# ============================================
# 2. 传感器声明 (使用内部 VREFINT 和模拟值)
# ============================================
# SENSOR temp FROM ADC1_CH0 SCALE 1.0 0.0   # 使用 VREFINT 读取
SENSOR temp CONST 25.0                       # 模拟温度传感器 (常数)
SENSOR enable CONST 1.0                      # 使能信号 (常数)

# ============================================
# 3. 比较运算
# ============================================
ARITH temp_high = temp GT threshold          # temp > 0.5 → 1.0
ARITH temp_ok   = temp GT 20.0 AND temp LT 30.0  # 20 < temp < 30 → OK

# ============================================
# 4. 定时器
# ============================================
TIMER t1: IN=enable, PT=2s → Q=system_ready   # 2 秒后 system_ready=1

# ============================================
# 5. 综合逻辑
# ============================================
LOGIC all_ok = temp_high AND system_ready AND enable

# ============================================
# 6. GPIO 输出 (输出到 PE0-PE3, 便于 LED 观察)
# ============================================
OUTPUT all_ok    TO GPIO_PE0     # PE0: 所有条件满足
OUTPUT temp_high TO GPIO_PE1     # PE1: 温度高
OUTPUT temp_ok   TO GPIO_PE2     # PE2: 温度在范围内
OUTPUT system_ready TO GPIO_PE3  # PE3: 系统就绪 (2秒后 ON)
