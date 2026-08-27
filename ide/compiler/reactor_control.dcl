# 反应釜温度-液位串级控制系统
# 复杂度: ~35 routes, 涵盖传感器/PID/定时器/计数器/报警/逻辑/输出

# === 常量 ===
CONST temp_sp = 75.0
CONST level_sp = 60.0
CONST kp = 2.5
CONST ki = 0.15
CONST kd = 0.08

# === 传感器 (6路) ===
SENSOR temp_raw FROM ADC1_CH0 SCALE 0.1 0.0
SENSOR level_raw FROM ADC1_CH1 SCALE 0.05 0.0
SENSOR pressure_raw FROM ADC1_CH2 SCALE 0.01 0.0
SENSOR inlet_flow FROM ADC1_CH3 SCALE 1.0 0.0
SENSOR outlet_flow FROM ADC1_CH4 SCALE 1.0 0.0
SENSOR ambient_temp FROM ADC1_CH5 SCALE 0.1 0.0

# === 信号滤波 ===
FILTER temp FROM temp_raw LOWPASS a=0.1
FILTER level FROM level_raw LOWPASS a=0.1
FILTER pressure FROM pressure_raw LOWPASS a=0.15

# === PID 控制 (2回路) ===
PID heat_out FROM temp SP=temp_sp KP=kp KI=ki KD=kd LIMIT 0 100
PID level_out FROM level SP=level_sp KP=1.5 KI=0.1 KD=0.05 LIMIT 0 100

# === 定时器 (3个) ===
TIMER heat_stable_t: IN=heat_out, PT=5s → Q=heat_stable
TIMER level_stable_t: IN=level_out, PT=8s → Q=level_stable
TIMER cycle_t: IN=heat_stable, PT=10s → Q=cycle_done

# === 计数器 (2个) ===
COUNTER batch_count: CU=cycle_done, PV=100 → Q=batch_full, CV=cycles
COUNTER flow_total: CU=inlet_flow, PV=500 → Q=tank_filled, CV=total_flow

# === 报警 (5个) ===
ALARM temp_hi FROM temp > 90
ALARM temp_lo FROM temp < 20
ALARM pressure_hi FROM pressure > 80
ALARM level_hi FROM level > 85
ALARM level_lo FROM level < 15

# === 逻辑 ===
LOGIC system_ready = heat_stable AND level_stable
LOGIC any_alarm = temp_hi OR temp_lo OR pressure_hi OR level_hi OR level_lo
LOGIC running = system_ready AND NOT any_alarm
LOGIC fill_complete = tank_filled AND cycle_done

# === 输出 (6路) ===
OUTPUT heat_out TO TIM1_CH1
OUTPUT level_out TO TIM1_CH2
OUTPUT running TO GPIO_PE0
OUTPUT any_alarm TO GPIO_PE1
OUTPUT batch_full TO GPIO_PE2
OUTPUT cycle_done TO GPIO_PE3
