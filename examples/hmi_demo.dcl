# hmi_demo.dcl — HMI 设定值演示 (A: DSL 引用 Modbus 设定区)
#
# 物理链 (上位机 → 引擎, 运行期可改):
#   上位机 Modbus 写 40065/40066 (u16, 工程量 ×100)
#     → MB_SET[0]/[1]
#       → 本程序的 hmi[...] 源读到 (÷100 还原)
#         → 参与运算 (这里是进入 PID 的设定值/报警阈值)
#
# 与 CONST 的区别: CONST 是部署时固定值; hmi 是"运行期可改的设定值" —
# 正是 PLC 的 HMI 设定值语义 (触摸屏/上位机改参数, 无需重新下载程序)。
#
# 运行: python tools/dclc.py examples/hmi_demo.dcl
# 试:   写 40065=6000 (60.00) → 温度设定值 60

SENSOR  fb       FROM sensor[2]                    # 反馈 (HIL ADC 或真实 AI 通道)
HMI     sp       FROM hmi[0]                       # ★设定值  ← 上位机写 40065 (×100)
HMI     hi_lim   FROM hmi[1]                       # ★报警上限 ← 上位机写 40066 (×100)

PID     heat     FROM fb  SP=50  KP=1.0  KI=5.0    # 温度环 (SP 固定 50, 演示用)
ALARM   hot      FROM fb > 80                      # 超温硬报警 (固定阈值)
LOGIC   ready    = hot AND hot                     # 演示 AND (单条件占位)

OUTPUT  pwm      TO wire[20] FROM heat             # PID 输出 → 执行器槽
OUTPUT  sp_out   TO wire[21] FROM sp               # 镜像设定值 (供上位机回读校验)
