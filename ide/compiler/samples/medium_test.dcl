# ================================================================
# DCL 中等复杂度测试 — 可观测 GPIO 输出
# 目的: 验证 GPIO 输出波形,测量实际抖动
#
# 场景: 反应釜温度 + 压力 + 液位 三回路控制
# 路由数: ~30 条
# 安全裕量: >50% (执行 <50μs)
#
# GPIO 观测点:
#   PE0 = 1Hz 方波 (每 100ms toggle)  → 看长期稳定性
#   PE1 = 10Hz 方波 (每 10ms toggle)  → 看中速稳定性
#   PE2 = 引擎心跳 (ISR toggle,最快)  → 看 ISR 周期
#   PE3 = 故障灯 (alarm ANY)           → 看逻辑正确
# ================================================================

# ── 输入: 3 路传感器 ──
SENSOR  temp     FROM ADC1_CH0    SCALE 0.01 0.0
SENSOR  press    FROM ADC1_CH1    SCALE 0.001 0.0
SENSOR  level    FROM ADC1_CH2    SCALE 0.1 0.0

# ── 信号调理: 3 路滤波 ──
FILTER  temp_f   FROM temp       LOWPASS a=0.1
FILTER  press_f  FROM press      LOWPASS a=0.15
FILTER  level_f  FROM level      LOWPASS a=0.2

# ── 控制: 2 路 PID + 1 路 ON/OFF ──
PID     heat_pid  FROM temp_f     SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
PID     cool_pid  FROM press_f    SP=40 KP=1.5 KI=0.08 KD  0.03 LIMIT 0 100
HYST    level_sw  FROM level_f HIGH 80 LOW 75

# ── 报警: 6 路 ──
ALARM   temp_hi   FROM temp_f > 85
ALARM   temp_lo   FROM temp_f < 10
ALARM   press_hi  FROM press_f > 70
ALARM   press_lo  FROM press_f < 5
ALARM   level_hi  FROM level_f > 95
ALARM   level_lo  FROM level_f < 20

# ── 逻辑综合 ──
LOGIC   any_alarm = temp_hi OR temp_lo OR press_hi OR press_lo OR level_hi OR level_lo
LOGIC   sys_ok    = NOT any_alarm
LOGIC   run_light = sys_ok

# ── 定时器 + 计数器 (用于 GPIO 观测) ──
TIMER   t_1hz:   IN=sys_ok, PT=1s → Q=q_1hz
TIMER   t_10hz:  IN=sys_ok, PT=100ms → Q=q_10hz
COUNTER cnt_1hz: CU=q_1hz, PV=1 → Q=toggle_1hz, CV=hz_count
COUNTER cnt_10hz: CU=q_10hz, PV=1 → Q=toggle_10hz, CV=tenhz_count

# ── 边沿检测 (产生精确 1Hz/10Hz 方波) ──
EDGE    edge_1hz   FROM toggle_1hz RISING
EDGE    edge_10hz  FROM toggle_10hz RISING

# ── GPIO 输出 (可观测) ──
OUTPUT  pe0_1hz   TO GPIO_PE0  FROM edge_1hz        # 1Hz 方波
OUTPUT  pe1_10hz  TO GPIO_PE1  FROM edge_10hz       # 10Hz 方波
OUTPUT  pe3_fault TO GPIO_PE4  FROM any_alarm       # 故障灯

# ── PWM 输出 (PID 结果,适合示波器) ──
OUTPUT  pwm_heat  TO TIM1_CH1  FROM heat_pid
OUTPUT  pwm_cool  TO TIM1_CH2  FROM cool_pid
