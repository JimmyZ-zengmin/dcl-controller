# h723_step_bounded_position.dcl — ★★★ 有界闭环定位（**环在片内、每拍**）
#
# ══════════════════════════════════════════════════════════════════════════
# 这个例程要展示的"架构优势" —— 三件，都是**可量化的**
# ══════════════════════════════════════════════════════════════════════════
# ① **100 µs 控制周期**：环路整条都在 ③ 层（引擎，**每拍执行**），
#    读编码器 → 算误差 → 钳位 → 下运动请求，全部在一拍内。
#    ★ 对照：PC 在环实测环频只有 **13.5 Hz / 滞后 ~80 ms**（审计 §9.1）⇒ **差约 700 倍**。
#    ★ 证据：`0x38` 的 `period_min/period_max`（每拍实测周期）与 `0x39 op=21` 的时基健康度。
# ② **抖动控制**：位置保持时**零漂移**（审计实测：3 s 漂移 0.000°、峰峰 1 LSB = 0.088°）。
#    ★ 本程序带**误差带自停**（|err| ≤ 0.5° ⇒ 频率 = 0）⇒ 到位后**不抖动、不发热**。
# ③ **有界（不失控）**：四道界同时生效 ——
#    第1道 **幅值钳位** `[HZ_LO, HZ_HI]`（★ 下限不用 TIM3 的 15 Hz：15 Hz 下电机 3.4°/s，
#                          看着像"不转"，而且会让"该反向"被下限**翻转成正向** ⇒ 发散）
#    第2道 **方向由误差符号决定**（双极性）—— 而不是把符号丢掉再钳位
#    第3道 **误差带自停**（带内 ⇒ 0，绝不"微小抖动持续通电"）
#    第4道 **限时安全网**（固件 `step_tick` 到点自动停脉冲，与程序解耦）
#
# ══════════════════════════════════════════════════════════════════════════
# ★★ 必须先做对的两件前置（都是实测踩出来的）
# ══════════════════════════════════════════════════════════════════════════
# (a) **反馈源**：`sensor[0]/[1]` 靠**具名设备绑定表**每 N 拍回填，而**绑定表随程序包持久化**
#     ⇒ 上传程序会把它删掉 ⇒ 上传后**必须先跑** `python tools/h723_as5600_bind.py`。
# (b) **运动源**：`0x39 op=19 sub=13 arg=1` 切到程序面（默认 0 = 脚手架直控）。
#
# ★ 用法：
#   python tools/dclc.py examples/h723_step_bounded_position.dcl
#   python tools/h723_as5600_bind.py            # ★ 部署后必做
#   # 然后 op=19 sub=13 arg=1 切程序面
# ══════════════════════════════════════════════════════════════════════════

# ── 参数 ──
CONST   tgt     = -40.0        # 目标角 —— ★ **折到 [-180,180) 表示**: -40° 即 320°
                               #   ★ 与反馈同域 ⇒ 减出来就是**最短弧**，环绕自动消失
CONST   kp      = 4.0          # P 增益 (Hz per deg)（12 时 7° 误差就冲过目标 ⇒ 会环绕）
CONST   HZ_LO   = 60.0         # 幅值下限：60 Hz = 13.5°/s（仍"看得见"；300 Hz 会一冲就过目标）
CONST   hz_hi   = 3000.0       # 幅度上限：远低于突加失速点（16650 Hz），留足余量
CONST   band    = 0.5          # 误差带 (°)：|err| ≤ band ⇒ 停（到位自停）
CONST   lim_ms  = 5000.0       # 限时安全网（固件侧，到点自动停）
CONST   one     = 1.0
CONST   mone    = -1.0

# ── 反馈：sensor[1] = AS5600 角度 0..360（sensor[0] 是 raw 0..4095）──
SENSOR  ang     FROM sensor[1]

# ── ★★★ 把单圈角度折到 [-180, 180)：这是**用原语凑出"最短弧"**
#    动机（实测踩过）：AS5600 是单圈 0..360，`err = tgt - ang` 在 ±180 处**跳变**
#    ⇒ 一块"冲过目标 3°"会被算成"反向 4°"，越绕越大 ⇒ 饱和 ⇒ **看起来像失控**。
#    DCL 没有 ABS/MOD，但 **ADD + GE + SEL 就够了**：ang ≥ 180 ⇒ 用 (ang − 360)。
CONST   n360    = -360.0
ADD     a2      FROM ang BY=n360        # ang − 360
GE      wrap    IN=ang THR=180.0        # ang ≥ 180 ⇒ 折
SEL     fbk     G=wrap IN0=ang IN1=a2   # → [-180, 180)

# ── 误差（带符号，**最短弧**）──
SUB     err     FROM tgt BY=fbk

# ── |err|：MAX(err, -err) —— DCL 没有 ABS，用 MAX 表达（★ 这就是"用原语凑出绝对值"）──
MUL     eneg    FROM err BY=mone
MAX     eab     FROM err BY=eneg

# ── 幅值 = kp×|err|，再钳到 [HZ_LO, hz_hi] ──
MUL     raw     FROM eab BY=kp
LIMIT   hz0     IN=raw MN=300.0 MX=3000.0

# ── 误差带门控：带外 ⇒ 1，带内 ⇒ 0 ⇒ 频率 0（自停）──
GE      drv     IN=eab THR=0.5
MUL     hz      FROM hz0 BY=drv

# ── ★ 方向 = 误差符号：err ≥ 0 ⇒ dir=0（实测 dir=0 ⇒ 角度增大）；err < 0 ⇒ dir=1 ──
GE      dpos    IN=err THR=0.0
LOGIC   dneg    = NOT dpos

# ── 运动请求（程序面：③层写 wire[12..15]）──
OUTPUT  o_hz    TO wire[12] FROM hz
OUTPUT  o_dir   TO wire[13] FROM dneg
OUTPUT  o_ena   TO wire[14] FROM one
OUTPUT  o_lim   TO wire[15] FROM lim_ms

# ── 观测（★ 64/65 是固件保留的运动镜像；60..63 供本程序用）──
OUTPUT  w_fbk   TO wire[59] FROM fbk
OUTPUT  w_err   TO wire[60] FROM err
OUTPUT  w_eab   TO wire[61] FROM eab
OUTPUT  w_hz    TO wire[62] FROM hz
OUTPUT  w_dir   TO wire[63] FROM dneg
