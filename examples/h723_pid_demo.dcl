# h723_pid_demo.dcl — 连续域(PID) 文本组态示例 (H723 原生)
#
# ★ 设计要点一: 反馈取自 `sensor[0]` —— 这个槽**不被任何组件回填**
#   (DI 写 sensor[3..6] / HIL 反馈写 sensor[2] / AI 写 sensor[8..10])，
#   所以上位机可以用 0x21 WRITE 直接注入**确定性试验信号**，
#   从而在**没有被控对象**的情况下验证 PID 的 方向性 / 速率 / 限幅 / 抗积分饱和。
#
# ★ 设计要点二: 这里取 **KP=0, KI=0.5** 而不是一组"好看的整定值" ——
#   因为本程序是**测量装置**不是工艺程序: KP≠0 时 P 项 `KP*err` 会一步就顶到限幅，
#   积分速率就被掩盖了。KP=0 让 u = KI·∫err dt，于是
#       err = +50 (fb=0, SP=50)  →  输出以 **25 /s** 上升  ← 可精确对照
#       err = -40 (fb=90)        →  输出以 **20 /s** 下降
#   这才能把"积分增益在文本组态里真的生效了"变成可断言的事实。
#
# PID 原语语义 (src/primitives.h:93): 位置式 + 梯形积分 + 微分; Ki 单位 /s, Kd 单位 s;
#   输出**内建限幅 [0, 100]**，且带**条件积分防 windup**
#   (err 方向与饱和方向相同时冻结积分 ⇒ 退出饱和必须是"立即"的，而不是等积分退完)。

SENSOR  fb      FROM sensor[0]                  # 试验信号输入 (上位机 0x21 注入)
PID     ctl     FROM fb  SP=50 KP=0.0 KI=0.5    # 纯积分环, 便于测速率
OUTPUT  duty    TO wire[20] FROM ctl            # 输出钉子 (与 HIL 的 PWM 输入槽一致)

# 饱和指示 (便于判据一次 burst 读回)
GE      sat_hi  IN=ctl THR=99.5
LE      sat_lo  IN=ctl THR=0.5
OUTPUT  oh      TO wire[21] FROM sat_hi
OUTPUT  ol      TO wire[22] FROM sat_lo
