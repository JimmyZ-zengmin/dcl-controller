# DCL测试程序: 温控系统
# 功能: 温度传感器 → 滤波 → PID → 限幅 → PWM输出
#       超温报警 → LED

SENSOR  temp_raw   FROM ADC1_CH0    SCALE 1.0 0.0
FILTER  temp_f     FROM temp_raw    LOWPASS a=0.1
PID     heater     FROM temp_f      SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
ALARM   overheat   FROM temp_f > 80
ALARM   undertemp  FROM temp_f < 10
LOGIC   fault      = overheat OR undertemp
OUTPUT  heat_pwm   TO TIM1_CH1      FROM heater
OUTPUT  fault_led  TO GPIO_PE5      FROM fault

TIMER   t1: IN=overheat, PT=3s → Q=alarm_delay
LATCH   sr1: S1=overheat, R=alarm_ack → Q1=alarm_latch