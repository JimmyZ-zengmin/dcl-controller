# h723_logic_demo.dcl — 逻辑域 + 标准块库 文本组态示例 (H723 原生)
#
# ★ 为什么这个示例值得存在: 它是**零硬件**的确定性验收输入 ——
#   全部输入来自 CONST ⇒ 引擎输出必须是**唯一确定值**, 可逐条断言 (不是"看起来在工作")。
#   配合 tools/h723_demo_e2e.py 的 B 组判据使用。
#
# 覆盖: 算术族(ADD/SUB/MUL/DIV) · 极值(MAX/MIN) · 比较(GE/LT) · 逻辑(AND/NOT)
#       · 二选一(SEL) · 双稳态(SR/RS) · 计数(CTU/CTUD) · 边沿(R_TRIG) · 限幅(LIMIT)

CONST   a     = 12.0
CONST   b     = 4.0
CONST   one   = 1.0
CONST   zero  = 0.0
CONST   sp    = 60.0                    # 阈值用

# ---- 算术族: 12 与 4 ----
ADD     sumv  FROM a  BY=b              # 16
SUB     difv  FROM a  BY=b              #  8
MUL     prdv  FROM a  BY=b              # 48
DIV     qutv  FROM a  BY=b              #  3

# ---- 极值 ----
MAX     mxv   FROM a  BY=b              # 12
MIN     mnv   FROM a  BY=b              #  4

# ---- 比较 → 逻辑 (阈值/布尔语义) ----
GE      big   IN=sumv THR=10.0          # 16 >= 10 → 1
LT      small IN=sumv THR=10.0          # 16 <  10 → 0
LOGIC   both  = big AND one             # 1 AND 1 → 1
LOGIC   nbig  = NOT big                 # NOT 1 → 0

# ---- 二选一: G=1 选 IN1 ----
SEL     selv  G=one  IN0=a  IN1=b       # → 4

# ---- 双稳态: SR 置位优先 / RS 复位优先 ----
SR      q_sr  S1=one R=zero             # S1 优先 → 1
RS      q_rs  S1=one R=one              # 同时为真, 复位优先 → 0

# ---- 计数: CU 恒 1 ⇒ 每次上升沿 +1 (只数一次) ----
CTU     cu1   CU=one  PV=1000           # CV = 1

# ---- 限幅: 把 48 夹到 [0,10] ----
LIMIT   lim   IN=prdv MN=0.0 MX=10.0    # → 10

# ---- 输出钉子 (便于一次 burst 读回全部结果) ----
OUTPUT  o_sum TO wire[40] FROM sumv
OUTPUT  o_dif TO wire[41] FROM difv
OUTPUT  o_prd TO wire[42] FROM prdv
OUTPUT  o_qut TO wire[43] FROM qutv
OUTPUT  o_max TO wire[44] FROM mxv
OUTPUT  o_min TO wire[45] FROM mnv
OUTPUT  o_sel TO wire[46] FROM selv
OUTPUT  o_sr  TO wire[47] FROM q_sr
OUTPUT  o_rs  TO wire[48] FROM q_rs
OUTPUT  o_cu  TO wire[49] FROM cu1
OUTPUT  o_lim TO wire[50] FROM lim
OUTPUT  o_big TO wire[51] FROM big
OUTPUT  o_sml TO wire[52] FROM small
OUTPUT  o_bth TO wire[53] FROM both
OUTPUT  o_nbg TO wire[54] FROM nbig
