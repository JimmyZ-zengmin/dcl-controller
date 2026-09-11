# hiloop.dcl — 引擎闭 H723 对象的恒温环 (示例程序)
# 物理链: hil 任务把 ADC(GPIO4) 读数写成 SENSOR[2]
#         → SCALE 标定(两点校正) → PID → wire[20] → hil 转 LEDC PWM → H723 加热对象
# 运行: python tools/dclc.py examples/hiloop.dcl      (先烧 hil 固件, 接好杜邦线)

SENSOR  fb      FROM sensor[2]                      # 引擎读反馈槽
SCALE   fb_cal  FROM fb      K=1.0105  B=2.339      # ADC 增益标定 (hil_calib 实测系数)
PID     heat    FROM fb_cal  SP=60  KP=1.0  KI=5.0  # 温度环: 输出限幅 0..100 (原语内建)
ALARM   hot   FROM fb_cal > 80                   # 超温报警 (1.0/0.0)
ALARM   warm  FROM fb_cal > 40                   # 预热完成
LOGIC   ready = hot AND warm                     # 演示 AND: 双条件同时满足(此时=hot)

OUTPUT  pwm   TO wire[20] FROM heat              # PID 输出钉到 hil 输入槽 wire20
