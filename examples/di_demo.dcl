# DI 数字量输入 demo — PLC "读输入 → 算逻辑 → 写输出" 完整三要素
# 硬件: 4×DI (GPIO1/2/3/9, 内部上拉, 按下/短接到 GND = 0, 悬空 = 1)
# 实测: 读 SENSOR[3..6] (di1..di4) 看电平; 短接 GPIO1 到 GND 观察 di1 翻转

# ---- 输入 (DI 组件每 10ms 扫描写入 SENSOR_MAP[3..6]) ----
SENSOR  di1     FROM sensor[3]       # GPIO1 (上拉: 悬空=1, 接地=0)
SENSOR  di2     FROM sensor[4]       # GPIO2
SENSOR  di3     FROM sensor[5]       # GPIO3
SENSOR  di4     FROM sensor[6]       # GPIO9

# ---- 逻辑 (按下为 0 的"低有效"解读: 未按下 → 按钮=1, 按下 → 0) ----
LOGIC   btn1    = NOT di1            # 按下 GPIO1 → btn1=1
LOGIC   btn2    = NOT di2            # 按下 GPIO2 → btn2=1

# ---- 联锁示例: 两个按钮同时按下 → 输出 ----
LOGIC   both    = btn1 AND btn2
OUTPUT  lamp    TO wire[30] FROM both

# ---- 计数示例: 按钮每按一次 +1 (演示 DI 与 FBD 计数块的连接) ----
R_TRIG  press   CLK=btn1             # GPIO1 按下的上升沿
CTU     presses CU=press PV=1000     # 按压计数
ALARM   many    FROM presses >= 5    # 按够 5 次
