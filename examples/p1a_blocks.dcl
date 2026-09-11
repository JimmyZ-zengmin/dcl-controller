# p1a_blocks.dcl — P1a: LPF/HYST/RATE/DEADBAND 语法口子 + LOGIC 链 + M5 预检
# 语义设计 (实机对拍见 tools/verify_p1a.py):
#   lpf  : 一阶惯性, src 阶跃 → 指数逼近 (t=τ 达 63%)
#   hyst : ON>OFF 滞回 — src 升破 ON → 1, 跌破 OFF → 0, 带内保持
#   rate : 每秒变化率 (dt 感知)
#   deadband: 变化过带才更新, 带内保持上一有效值
#   lgc3 : 3 输入 AND 链
# 源: wire[100] 为无生产者测试输入 (0x21 驱动, 同 verify_seq 的 WA/WB 模式)

LPF     lpf1    FROM wire[100] T=0.1   # τ=0.1s
HYST    hys1    FROM wire[100] ON=60 OFF=40
RATE    rt1     FROM wire[100]
DEADBAND db1    FROM wire[100] B=0.5

CONST   a       = 1.0
CONST   b       = 1.0
CONST   c       = 1.0
LOGIC   lgc3    = a AND b AND c        # 3 输入 AND 链 → 1
LOGIC   lgc_o   = a OR b               # 2 输入 (回归)
LOGIC   xr      = a XOR b              # XOR 回归
LOGIC   ng      = NOT a                # NOT 回归
