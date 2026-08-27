# DCL全功能测试 — 金属压铸机模拟
# 测试全部12种IEC 61131-3 FB类型

# ── 输入 ──
SENSOR  temp         FROM ADC1_CH0    SCALE 1.0 0.0
SENSOR  pressure    FROM ADC1_CH1    SCALE 0.1 0.0

# ── 信号调理 ──
FILTER  temp_f       FROM temp        LOWPASS a=0.1
FILTER  press_f      FROM pressure    LOWPASS a=0.3
RATE    temp_rate    FROM temp_f
SCALE   temp_pct     FROM temp_f      RANGE 0 150

# ── 控制算法 ──
PID     heater       FROM temp_f      SP=180 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
PID     press_ctrl   FROM press_f     SP=50  KP=1.5 KI=0.2 KD  0.02 LIMIT 0 100

# ── 逻辑/报警 ──
ALARM   overtemp     FROM temp_f > 200
ALARM   lowpress     FROM press_f < 10
ALARM   temp_rising  FROM temp_rate > 5
LOGIC   fault        = overtemp OR lowpress
LOGIC   sys_ready    = NOT fault

# ── 定时器 ──
TIMER   warmup:      IN=sys_ready, PT=10s → Q=warmup_done
TIMER   inject_dly:  IN=warmup_done, PT=500ms → Q=inject_start

# ── 计数器 ──
COUNTER cycle:       IN=inject_start, PV=1000 → Q=maintenance_needed, CV=cycle_count

# ── 双稳态 ──
LATCH   sys_run:     S1=sys_ready, R=fault → Q1=system_running

# ── 输出 ──
OUTPUT  heater_pwm   TO TIM1_CH1      FROM heater
OUTPUT  press_valve  TO TIM1_CH2      FROM press_ctrl
OUTPUT  run_led      TO GPIO_PE4      FROM system_running
OUTPUT  fault_buzzer TO GPIO_PE5      FROM fault
