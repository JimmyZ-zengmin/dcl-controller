# ================================================================
# Tank Level Control System — 中等复杂度 PLC 程序
# ================================================================
# 功能:
#   1. 2路传感器输入 (电感式液位 + 温度)
#   2. 移动平均滤波 (LPF)
#   3. PID 闭环控制进水阀
#   4. 高低液位报警 + 温度报警 (CMP)
#   5. 报警后的延时确认 (TIMER)
#   6. 设备运行统计 (COUNTER)
#   7. 自动/手动模式切换 (LATCH SR)
#   8. 输出: PWM进水电阀 + PWM冷却阀 + 3路GPIO指示灯
#
# 总计: ~22条路由, 用到 12+ 种原语
# ================================================================

# ── 输入 ──
SENSOR  level       FROM ADC1_CH0    SCALE 0.01 0.0       # 液位传感器 (0~4V = 0~100cm)
SENSOR  temp        FROM ADC1_CH1    SCALE 0.02 0.0       # 温度传感器 (0~3.3V = 0~165°C)

# ── 信号调理 ──
FILTER  level_f     FROM level       LOWPASS a=0.05        # 液位滤波 (α=0.05, τ≈2s @100Hz)
FILTER  temp_f      FROM temp        LOWPASS a=0.1         # 温度滤波

# ── 控制算法 ──
PID     level_ctrl  FROM level_f     SP=60 KP=3.0 KI=0.15 KD=0.2 LIMIT 0 100   # 液位 PID: 目标 60cm
PID     temp_ctrl   FROM temp_f      SP=50 KP=2.0 KI=0.1  KD  0.05 LIMIT 0 100 # 温度 PID: 目标 50°C

# ── 报警逻辑 ──
ALARM   level_hi    FROM level_f > 90.0                   # 高液位
ALARM   level_lo    FROM level_f < 10.0                   # 低液位
ALARM   temp_hi     FROM temp_f  > 120.0                  # 超温
ALARM   sensor_err  FROM level_f < 0.5                    # 传感器断线 (读数为0)

# ── 逻辑综合 ──
LOGIC   fault       = level_hi OR level_lo OR temp_hi OR sensor_err   # 任一异常
LOGIC   sys_ready   = NOT fault                                        # 系统无故障
LOGIC   filling     = level_ctrl > 5.0                                 # 正在进水
LOGIC   cooling     = temp_ctrl > 10.0                                 # 正在冷却

# ── 延时确认 ──
# 故障持续 2 秒才输出给操作站 (防抖)
TIMER   fault_dly:  IN=fault, PT=2000ms → Q=fault_confirmed, ET=fault_et

# ── 周期统计 ──
# 每 filling 上升沿计数一次, PV=1000 次后提醒维护
COUNTER fill_cycle:  CU=filling, PV=1000 → Q=maint_needed, CV=fill_count

# ── 模式切换 (自锁继电器) ──
# sys_ready 上升沿锁存启动, 故障确认后复位
LATCH   sys_run:     S1=sys_ready, R=fault_confirmed → Q1=system_running

# ── 输出 ──
# PWM 输出 (0~100% → TIM1 136MHz/13600 = 10kHz PWM)
OUTPUT  fill_valve   TO TIM1_CH1      FROM level_ctrl               # 进水电阀
OUTPUT  cool_valve   TO TIM1_CH2      FROM temp_ctrl                # 冷却阀

# GPIO 数字输出
OUTPUT  run_led      TO GPIO_PE3      FROM system_running           # 运行指示灯(绿)
OUTPUT  fault_led    TO GPIO_PE4      FROM fault_confirmed           # 故障灯(红)
OUTPUT  fill_led     TO GPIO_PE5      FROM filling                   # 进水中(黄)
