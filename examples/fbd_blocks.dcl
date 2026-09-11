# FBD 标准块第一批 — 定时器/计数器/边沿/限幅/比较/异或
# 用途: 验证 dclc v0.1 新语法 + 引擎 TIMER/CNT/EDGE/CLAMP/CMP 语义
# 实机观测: 用 0x22 读 wire 槽, 或用 dclmon(待建) 看随时间变化

# ---- 常量源 (SRC_CONST) ----
CONST   one     = 1.0                   # 恒真 (当作一个一直按着的按钮)
CONST   zero    = 0.0                   # 恒假 (当作没按复位)

# ---- 定时器族 (IEC: TON/TOF/TP) ----
# 注: PT 取得比"刚好够观测"大, 是因为 PC 侧串口单次往返实测 0.3~1.5s (协议响应慢),
# 秒级时序判据必须留足余量, 否则采样点会晚于名义时刻导致误判。
TON     ton1    IN=one  PT=6s           # 6 秒后 Q=1 (并保持)
TOF     tof1    IN=one  PT=3s           # IN=1 立即 Q=1 (IN 变 0 后延时 3s 才落)
TP      tp1     IN=one  PT=8s           # 上升沿触发 8 秒宽脉冲, 之后归 0

# ---- 计数器族 (IEC: CTU/CTD) ----
CTU     ctu1    CU=one  R=zero  PV=5    # 上升沿累加; R=1 清零; 输出 CV
CTD     ctd1    CD=one  LD=zero PV=5    # 上升沿递减; LD=1 装 PV; 输出 CV

# ---- 边沿检测 (IEC: R_TRIG/F_TRIG) ----
R_TRIG  rt1     CLK=one                 # 上升沿 → 1 (仅一拍)
F_TRIG  ft1     CLK=one                 # 下降沿 → 1 (仅一拍)

# ---- 限幅与比较 (IEC: LIMIT / 比较族) ----
LIMIT   lim1    IN=one  MN=0  MX=100    # clamp(1, 0, 100) = 1
GE      ge1     IN=lim1 THR=1           # lim1 >= 1 → 1
LT      lt1     IN=lim1 THR=50          # lim1 <  50 → 1
ALARM   eq1     FROM lim1 >= 1          # 老语法: 现在编译成真 GE (修审计 M3)

# ---- 逻辑 ----
LOGIC   xr1     = ge1 XOR lt1           # 1 XOR 1 = 0 (组合实现)
