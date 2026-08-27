# ================================================================
# DCL 综合压测程序
# 目的: 验证引擎稳定性 + 测量抖动 + 压力测试
#
# 覆盖原语: SENSOR, FILTER, PID, CMP, ALARM, LOGIC, EDGE,
#           COUNTER, TIMER, MUX, ARITH, LIMIT, MAX, MIN, ABS,
#           LATCH, BIT, LUT, CONST, NOT, AND, OR, ADD, SUB, MUL, DIV
#
# 复杂度: ~50 条路由
# ================================================================

# ── 输入: 3 路传感器 ──
SENSOR  temp     FROM ADC1_CH0    SCALE 0.01 0.0
SENSOR  press    FROM ADC1_CH1    SCALE 0.001 0.0
SENSOR  level    FROM ADC1_CH2    SCALE 0.1 0.0

# ── 信号调理: 3 路滤波 ──
FILTER  temp_f   FROM temp       LOWPASS a=0.1
FILTER  press_f  FROM press      LOWPASS a=0.05
FILTER  level_f  FROM level      LOWPASS a=0.2

# ── 控制算法: 3 路 PID ──
PID     temp_pid FROM temp_f      SP=60 KP=2.0 KI=0.15 KD=0.05 LIMIT 0 100
PID     press_pid FROM press_f    SP=50 KP=1.5 KI=0.1  KD  0.02 LIMIT 0 100
PID     level_pid FROM level_f    SP=80 KP=3.0 KI=0.2  KD  0.1  LIMIT 0 100

# ── 报警: 6 路比较器 ──
ALARM   temp_hi    FROM temp_f > 90
ALARM   temp_lo    FROM temp_f < 10
ALARM   press_hi   FROM press_f > 80
ALARM   press_lo   FROM press_f < 5
ALARM   level_hi   FROM level_f > 95
ALARM   level_lo   FROM level_f < 20

# ── 安全逻辑: AND/OR/NOT ──
LOGIC   any_hi     = temp_hi OR press_hi OR level_hi
LOGIC   any_lo     = temp_lo OR press_lo OR level_lo
LOGIC   sys_fault  = any_hi OR any_lo
LOGIC   sys_ok     = NOT sys_fault
LOGIC   run_permit = sys_ok AND NOT sys_fault

# ── 定时器 + 边沿检测 ──
EDGE    run_pulse  FROM run_permit RISING
TIMER   startup_dly: IN=sys_ok, PT=1s Q=sys_ready
COUNTER run_cycles: CU=run_pulse, PV=1000 → Q=maint_needed, CV=cycle_count

# ── 数学运算: 加法/减法/乘/除 ──
CONST   k_p Gain   = 2.5
ARITH   temp_scaled   = temp_f  MUL Gain
ARITH   press_diff    = press_f SUB level_f
ARITH   sum_all       = temp_f  ADD press_f ADD level_f
ARITH   flow_rate     = press_f DIV temp_f

# ── 范围限幅 ──
LIMIT   temp_safe   FROM temp_f    RANGE -40 150
LIMIT   press_safe  FROM press_f   RANGE 0 100
LIMIT   out_clamped  FROM temp_pid  RANGE 0 100

# ── 极值选择 ──
MAX     max_temp    = MAX temp_f 0.0
MIN     min_temp    = MIN temp_f 100.0
ABS     abs_diff    = ABS press_diff

# ── 二选一 (自动/手动模式) ──
MUX     temp_out = run_permit SELECT temp_pid ELSE press_pid
MUX     level_out = sys_ok SELECT level_pid ELSE k_p Gain

# ── 锁存器: 故障锁存 ──
LATCH   fault_latch: S1=sys_fault, R=sys_ok → Q1=fault_held

# ── 位运算 ──
BIT     bit_mask    = temp_f BITAND press_f
BIT     bit_invert  = BITNOT temp_f
BIT     bit_combine = temp_f BITOR press_f

# ── 输出: GPIO 数字 + PWM 模拟 ──
OUTPUT  run_led     TO GPIO_PE3      FROM sys_ready
OUTPUT  fault_led   TO GPIO_PE4      FROM fault_held
OUTPUT  heat_pwm    TO TIM1_CH1      FROM temp_out
OUTPUT  cool_pwm    TO TIM1_CH2      FROM level_out
OUTPUT  press_valve TO TIM1_CH3      FROM press_safe
