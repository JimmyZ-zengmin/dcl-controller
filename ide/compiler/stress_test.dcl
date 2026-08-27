# S7-1200复杂度对标测试程序
# 目标: 模拟中等复杂度的PLC程序
# 包含: 多传感器、多PID、多定时器/计数器、逻辑运算

# ============================================
# 1. 常量声明
# ============================================
CONST temp_setpoint = 60.0
CONST pressure_setpoint = 50.0
CONST speed_setpoint = 1000.0
CONST level_setpoint = 80.0
CONST kp = 2.0
CONST ki = 0.1
CONST kd = 0.05

# ============================================
# 2. 传感器声明 (8个)
# ============================================
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
SENSOR pressure FROM ADC1_CH1 SCALE 0.01 0.0
SENSOR speed FROM ADC1_CH2 SCALE 1.0 0.0
SENSOR level FROM ADC1_CH3 SCALE 1.0 0.0
SENSOR flow FROM ADC1_CH4 SCALE 1.0 0.0
SENSOR voltage FROM ADC1_CH5 SCALE 0.001 0.0
SENSOR current FROM ADC1_CH6 SCALE 0.001 0.0
SENSOR vibration FROM ADC1_CH7 SCALE 1.0 0.0

# ============================================
# 3. 信号处理 (滤波)
# ============================================
FILTER temp_f FROM temp LOWPASS a=0.1
FILTER pressure_f FROM pressure LOWPASS a=0.1
FILTER speed_f FROM speed LOWPASS a=0.2
FILTER level_f FROM level LOWPASS a=0.1

# ============================================
# 4. PID控制 (4个回路)
# ============================================
PID temp_ctrl FROM temp_f SP=temp_setpoint KP=kp KI=ki KD=kd LIMIT 0 100
PID pressure_ctrl FROM pressure_f SP=pressure_setpoint KP=kp KI=ki KD=kd LIMIT 0 100
PID speed_ctrl FROM speed_f SP=speed_setpoint KP=kp KI=ki KD=kd LIMIT 0 100
PID level_ctrl FROM level_f SP=level_setpoint KP=kp KI=ki KD=kd LIMIT 0 100

# ============================================
# 5. 定时器 (4个)
# ============================================
TIMER t1: IN=temp_ctrl, PT=3s → Q=temp_stable
TIMER t2: IN=pressure_ctrl, PT=5s → Q=pressure_stable
TIMER t3: IN=speed_ctrl, PT=2s → Q=speed_stable
TIMER t4: IN=level_ctrl, PT=4s → Q=level_stable

# ============================================
# 6. 计数器 (4个)
# ============================================
COUNTER c1: CU=flow, PV=1000 → Q=flow_full, CV=flow_count
COUNTER c2: CU=vibration, PV=500 → Q=vib_alarm, CV=vib_count
COUNTER c3: CU=voltage, PV=200 → Q=voltage_ok, CV=volt_count
COUNTER c4: CU=current, PV=100 → Q=current_ok, CV=curr_count

# ============================================
# 7. 报警逻辑
# ============================================
ALARM temp_high FROM temp > 80
ALARM pressure_high FROM pressure > 70
ALARM speed_high FROM speed > 1200
ALARM level_low FROM level < 20
ALARM flow_low FROM flow < 10
ALARM voltage_high FROM voltage > 250
ALARM current_high FROM current > 15
ALARM vibration_high FROM vibration > 100

# ============================================
# 8. 综合逻辑
# ============================================
LOGIC system_ready = temp_stable AND pressure_stable AND speed_stable AND level_stable
LOGIC any_alarm = temp_high OR pressure_high OR speed_high OR level_low OR flow_low OR voltage_high OR current_high OR vibration_high
LOGIC fault = any_alarm OR NOT system_ready
LOGIC production_ok = system_ready AND NOT any_alarm AND flow_full AND voltage_ok AND current_ok

# ============================================
# 9. 输出 (8个)
# ============================================
OUTPUT temp_ctrl TO TIM1_CH1
OUTPUT pressure_ctrl TO TIM1_CH2
OUTPUT speed_ctrl TO TIM1_CH3
OUTPUT level_ctrl TO TIM1_CH4
OUTPUT system_ready TO GPIO_PE0
OUTPUT fault TO GPIO_PE1
OUTPUT production_ok TO GPIO_PE2
OUTPUT flow_full TO GPIO_PE3
