# ================================================================
# PWM + GPIO 验证程序
# 测试引擎计算 → ACT[] → PWM/GPIO 输出全链路
#
# 目标:
#   1. PID 输出驱动 3 路 PWM
#   2. ALARM 状态驱动 3 路 GPIO
#   3. 看示波器验证波形
#
# 场景: 温度控制 + 压力监控
#
# 输入 (内部 VREFINT,实际读 ~0.04V,远低于 SP)
# → PID 全开 (100%)
# → 报警位全部触发
# → 3 路 PWM = 100%,3 路 GPIO = HIGH
# ================================================================

# ── 输入 ──
SENSOR  temp         FROM ADC1_CH0    SCALE 1.0 0.0       # VREFINT (内部 ~1.2V → 实际 ~0.04V 读值)

# ── 控制 ──
FILTER  temp_f       FROM temp        LOWPASS a=0.1
PID     heater       FROM temp_f      SP=50 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
PID     cooler       FROM temp_f      SP=50 KP=1.5 KI=0.08 KD  0.02 LIMIT 0 100

# ── 报警 ──
ALARM   temp_low     FROM temp_f < 10
ALARM   overheat     FROM temp_f > 80

# ── 逻辑 ──
LOGIC   any_alarm    = temp_low OR overheat

# ── 输出: PWM ──
OUTPUT  pwm1         TO TIM1_CH1      FROM heater               # PE9
OUTPUT  pwm2         TO TIM1_CH2      FROM cooler               # PE11
OUTPUT  pwm3         TO TIM1_CH3      FROM any_alarm             # PE13 (报警时=100%)

# ── 输出: GPIO ──
OUTPUT  led_run      TO GPIO_PE4      FROM any_alarm             # 运行指示灯
OUTPUT  led_heat     TO GPIO_PE5      FROM heater                # 加热指示灯
OUTPUT  led_cool     TO GPIO_PE6      FROM cooler                # 冷却指示灯
