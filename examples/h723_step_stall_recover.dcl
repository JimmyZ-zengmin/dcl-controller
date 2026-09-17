# h723_step_stall_recover.dcl — ★★★ 闭环**失步自动检测 + 恢复正常转动**
#
# ══════════════════════════════════════════════════════════════════════════
# 它解决的是哪个真实故障
# ══════════════════════════════════════════════════════════════════════════
# 阶段 2 实测（2026-09-17，`tools/h723_stall_edge.py`）：**从静止"突加"到 >16500 Hz 必失速**
#   —— 转子跟不上，**持续通电流、嗡嗡响、轴不动**（0.000 比值 / 编码器单步仅 0.09°）。
#   拐点 16500~16750 Hz；而"已在转动时突跳到 31 kHz"完全正常 ⇒ 失效**只**发生在"从静止起转"。
# ★ 这个程序做的正是"**发现失速 → 把它救回转动**"，而不是等现场看到"响+不转+发热"。
#
# ══════════════════════════════════════════════════════════════════════════
# 判据（**每条都能失败**，且都写在可读回的槽里）
# ══════════════════════════════════════════════════════════════════════════
# ① **失步判据**：`dem > 0`（命令在跑）**且** `|每拍角度增量| < 0.25 × 期望增量`，
#    经 **TON 去抖 200 ms** 才确认。★ 用"增量"而不是"位置"——因为这里是**连续转动**场景，
#    位置一直在变，只有**增量**才是"跟不跟得上"的量。
# ② **期望值来自同一套标定**：期望增量 = `dem × 2.56e-4` counts/拍
#    （1600 步/圈、4096 counts/圈、10 kHz 拍 ⇒ `dem/1600×4096/10000`）。
#    ★ 注意这里**不引"请求频率"当结论**：`dem` 只用来算期望，实测值来自编码器本身。
# ③ **恢复动作**：`TP` 断流 150 ms（让转子落回同步）→ 恢复后按**降额**重发。
#    ★ 降额用 `CTU` **锁存**、由"主机重新下令"(`F_TRIG running`) 复位 ——
#      **不做"好了就立刻提回 100%"**：那会在失速边界上来回追（极限环）。
#
# ══════════════════════════════════════════════════════════════════════════
# 如实声明的边界（不藏）
# ══════════════════════════════════════════════════════════════════════════
# ① **环绕**：编码器是**单圈**。本程序把 `sensor[0]`(0..4095) 折到 ±2048 后做差分，
#    并把差分再折到 ±2048 ⇒ **只要每拍增量 < 半圈（1800 Hz 以上都满足）就无歧义**。
#    10 kHz 拍下每拍最多能表示 1800 counts/拍 = 17.6 M counts/s ⇒ **远超本台能力**。
# ② **只认"完全没跟上"，不认"少走一点"**：门限 0.25 是"几乎不动"的量级。
#    轻微丢步（比如 10%）**不会**触发 —— 那种场景该用位置环（见 bounded_position 例程）。
# ③ **降额后要恢复满速，必须主机重新下令**（把 hmi[0] 写成 0 再写回）。
#    ★ 这是刻意的：否则会变成"提上去→失速→降下来→提上去"的极限环。
# ④ 起转后 **900 ms 遮蔽窗**：固件斜坡要 ~0.5 s 到速，期间"增量小"是正常的，不能当失速。
#    ★ 若主机在**运行中**把频率从低改高（不经过 0），本程序不会开遮蔽窗 ⇒ 斜坡期间可能误判一次。
#      已知缺口；绕过办法：改频时先写 0 再写目标值。
#
# ★★ 用法
#   python tools/dclc.py examples/h723_step_stall_recover.dcl
#   python tools/h723_as5600_bind.py          # ★ 部署后必做（绑定表随程序包被清掉）
#   0x39 op=19 sub=13 arg=1                   # 运动源 = 程序面
#   写 HMI：Modbus 40065 = dem(Hz)，40066 = enarq
#   ★ 收尾：sub=13 arg=0（切回脚手架直控），否则脚手架设的频率会被每圈覆盖
#
# ══════════════════════════════════════════════════════════════════════════

# ── 常量 ──
CONST   one     = 1.0
CONST   mone    = -1.0
CONST   R4096   = -4096.0      # raw 折 ±2048
CONST   WSP     = 40960000.0   # 一圈: 4096 counts / 100µs
CONST   WSPN    = -40960000.0  # ★ 负一圈（`BY=` 只能接**已声明的 CONST 名或信号**）
CONST   K_TK    = 0.0001       # counts/s → counts/拍   ★ CONST 字面量**不认科学计数法**(实测),
CONST   K_EXP   = 0.000256     # Hz → counts/拍   (= 2.56 counts/step ÷ 10000 拍/s) —— 只能写小数
CONST   RATIO   = 0.25         # "没跟上"的比例门
CONST   DER1    = 0.25         # 降额一档
CONST   lim_ms  = 0.0          # 不限时（由本程序自己管停机）
CONST   dirfwd  = 0.0          # ★ `FROM` 只接受**已声明的信号/CONST**（写 `FROM 0.0` 会报"未定义信号"）
# ★★ 三条口径差异（2026-09-17 编译期实测，别再猜）：
#   · `THR=` 只认**数字字面量**（CONST 名/信号都不行）⇒ 拿算出来的量当门限要先 `SUB` 再比 0
#   · `BY=`  只认**已声明的 CONST 名或信号**（内联字面量不行）⇒ 负数也要先 CONST 出来
#   · `CONST` 字面量不认科学计数法（`1.0e-4` 报语法错误）⇒ 写 `0.0001`

# ── 上位机请求（HMI 设定区，写 40065+n 即生效）──
HMI     dem     FROM hmi[0]    # 请求频率 Hz（0 = 停）
HMI     enarq   FROM hmi[1]    # 请求使能

# ── 反馈：raw 0..4095 折到 [-2048, 2048) ──
#    ★★ 踩坑备忘（2026-09-17 实测）：`CONST` 与 `THR=` 的口径**不一样** ——
#      `THR=` 只接受**数字字面量**，既不认 CONST、也不认信号（写 CONST 名直接报语法错误）。
#      ⇒ 凡"拿一个**算出来的量**当门限"的地方，都必须先 `SUB` 造差值、再与 `0` 比。
SENSOR  raw     FROM sensor[0]
ADD     r2      FROM raw BY=R4096
GE      rw      IN=raw THR=2048.0
SEL     f0      G=rw IN0=raw IN1=r2

# ── 每拍增量（counts/s），并**双侧折衷**到 ±2048 counts/拍 等价区间 ──
#    为什么必须折：编码器单圈 ⇒ 过零时 RATE 会给出 ±4096 counts/拍 的假尖峰，
#    而它看起来**正好像"在动"** ⇒ 会把失速判据在整个转动过程中反复打掉（假阴性）。
RATE    dr      FROM f0
ADD     dpos    FROM dr BY=WSPN
GE      gph     IN=dr THR=20480000.0
SEL     d1      G=gph IN0=dr IN1=dpos
ADD     dneg    FROM d1 BY=WSP
LT      gln     IN=d1 THR=-20480000.0
SEL     d2      G=gln IN0=d1 IN1=dneg

# ── 换算成 counts/拍 并取幅值（DCL 无 ABS ⇒ 用 MAX(x, −x) 凑）──
MUL     dtk     FROM d2 BY=K_TK
MUL     dng     FROM dtk BY=mone
MAX     dab     FROM dtk BY=dng

# ── 期望增量与门限 ──
MUL     expc    FROM dem BY=K_EXP
MUL     lo      FROM expc BY=RATIO

# ── ① 失步判据（含起转遮蔽窗）──
GE      running IN=dem THR=1.0
SUB     dfl     FROM dab BY=lo              # ★ 差值口径：dab − 门限
LT      slow    IN=dfl THR=0.0              # 差值为负 ⇔ 实测 < 门限
LOGIC   bad0    = running AND slow
R_TRIG  rstart  CLK=running
TP      blank   IN=rstart PT=900ms          # 起转后遮蔽（固件斜坡期间不判）
LOGIC   nbl     = NOT blank
LOGIC   bad     = bad0 AND nbl
TON     stall   IN=bad PT=200ms             # ★ 去抖：连续 200 ms 才确认失步

# ── ③ 恢复：断流一瞬 + 降额重发（降额锁存，主机重下令才复位）──
TP      cut     IN=stall PT=150ms           # 断流窗（让转子落回同步）
F_TRIG  hdrop   CLK=running                 # 主机把 dem 写 0 ⇒ 这是一次"重新下令"
CTU     nfail   CU=stall R=hdrop PV=1
SEL     der     G=nfail IN0=one IN1=DER1    # ★ 直接用 nfail 当 G（SEL 按 >0.5 判真）
MUL     demd    FROM dem BY=der
LOGIC   ncut    = NOT cut
MUL     hz      FROM demd BY=ncut           # ★ 最终下发的频率（断流窗内 = 0）

# ── 运动请求（③层输出面**只有** `wire[]`）──
OUTPUT  o_hz    TO wire[12] FROM hz
OUTPUT  o_dir   TO wire[13] FROM dirfwd     # 方向固定为正（单向连续转动）
OUTPUT  o_ena   TO wire[14] FROM enarq      # ★ HMI 已是 0/1 ⇒ 不再过一道 GE（省一个 wire 槽）
OUTPUT  o_lim   TO wire[15] FROM lim_ms

# ── 观测（56..63 供本程序用；★ 64/65 是**固件保留**的运动镜像，程序不得占用）──
#    ★★ 2026-09-17 实测限制：引擎里**每个 `CONST` 也占一个 wire 槽**（第二输入管道按 wire 索引
#       取数）⇒ `dclc` 的自动分配上限 64 槽对"中等规模"程序就不够。本程序已按提示精简
#       （THR 内联字面量、不再为 `GE ena`/`GE nfail≥1` 单开信号），并砍到 4 个观测槽。
#       要再扩：走 HMI 设定区，或让引擎支持"常量不进 wire"（①层改动）。
OUTPUT  w_dab   TO wire[56] FROM dab        # 实测每拍增量 counts/拍
OUTPUT  w_stall TO wire[57] FROM stall      # 失步已确认（1 = 是）
OUTPUT  w_nfail TO wire[58] FROM nfail      # 失步次数（锁存）
OUTPUT  w_der   TO wire[59] FROM der        # 当前降额系数
OUTPUT  w_hz    TO wire[60] FROM hz         # 实际下发的频率
