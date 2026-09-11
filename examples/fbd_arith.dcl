# FBD 第二批 — 算术 / 极值 / 双稳态 / CTUD / SEL / div 分档
# 用途: 验证 dclc v0.2 新语法 + 引擎 ARITH(0x0D) 与 SR(0x12) 新原语

CONST   a     = 12.0
CONST   b     = 4.0
CONST   one   = 1.0
CONST   zero  = 0.0

# ---- 算术族 (IEC: ADD/SUB/MUL/DIV) 与极值 (MAX/MIN) ----
ADD     sumv  FROM a  BY=b          # 12 + 4 = 16
SUB     difv  FROM a  BY=b          # 12 - 4 = 8
MUL     prdv  FROM a  BY=b          # 12 * 4 = 48
DIV     qutv  FROM a  BY=b          # 12 / 4 = 3
MAX     mxv   FROM a  BY=b          # max(12,4) = 12
MIN     mnv   FROM a  BY=b          # min(12,4) = 4

# ---- 双稳态 (IEC: SR 置位优先 / RS 复位优先) ----
SR      q_sr  S1=one  R=zero        # S1=1 R=0 → 1
RS      q_rs  S1=one  R=one         # 同时为真, 复位优先 → 0

# ---- 加减计数 (IEC: CTUD) ----
CTUD    cud   CU=one  CD=zero PV=10 # 一次上升沿 → CV=1 (CD 恒 0 不减)

# ---- 二选一 (IEC: SEL) ----
SEL     selv  G=one  IN0=a  IN1=b   # G=1 → 选 IN1 = 4

# ---- div 分档: 10ms 慢档链路 ----
# 引擎速率语义 (T17/T18): 慢档路由不允许读更快档的 wire (pd < rd 即 NAK),
# 所以慢档链路必须自带慢档源 — 这也正是"多周期档"的正确用法: 整条慢回路同档。
CONST   a2    = 12.0  PERIOD=10ms
CONST   b2    = 4.0   PERIOD=10ms
ADD     slow  FROM a2 BY=b2 PERIOD=10ms      # 慢档 12+4 = 16
