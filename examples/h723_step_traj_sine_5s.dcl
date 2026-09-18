# h723_step_traj_sine_5s.dcl — 阶段 1 判据 2 的**片内臂**：0.2 Hz 速度阶梯正弦
#
# ★★ 与 `h723_step_traj_sine.dcl` **唯一差别 = 段长 35ms → 833ms**
#    （35ms×6 = 0.21 s ⇒ f = 4.76 Hz，那是阶段 3A 的"环装得下"版；
#      833ms×6 = 4998 ms ≈ **5.0 s** ⇒ **f = 0.20008 Hz**，这是判据 2 要的 0.2 Hz）
#
# ══════════════════════════════════════════════════════════════════════════
# 为什么需要这个文件（判据 2 的公平性要求，见 docs/PLAN-closedloop-stepper-v2.md 阶段 1-2）
# ══════════════════════════════════════════════════════════════════════════
# ① `track sine` 量的是**开环前馈跟随**（只按自己的钟下发 rate，不开位置环）
#    ⇒ PC 臂的误差 = **命令通路运输滞后** ⇒ 位置滞后 = ω × lag（lag 实测 87 ms）
#    ⇒ 滞后项 = 270 °/s × 0.087 s = **23.5°**（这是"收益"要消掉的那个固定项）
# ② ★ DCL **没有 `sin`**（`LUT` 原语引擎有、`dclc` 未暴露）⇒ 片内只能下发**阶梯表**
#    ⇒ **PC 臂必须用同一张阶梯表**（套件新增 `track stair6`）才可比 —— 这就是"唯一变量"的保证
# ③ ★ 尺度宽松：f = 0.2 Hz ⇒ 周期 5 s ⇒ **12 Hz 的 PC 采样通路绰绰有余**
#    ⇒ **不需要黑匣子（环只留 ~0.4 s）、不需要高速** —— 这是这条判据能测的根本原因
#
# ══════════════════════════════════════════════════════════════════════════
# 表：6 段阶梯，归一化因子 = sin(π·(k+0.5)/6)
# ══════════════════════════════════════════════════════════════════════════
#   段 k :  0      1      2      3      4      5
#   sin  : 0.259  0.707  0.966  0.966  0.707  0.259
#   ★ 直流分量（均值）= 0.644；交流峰峰（0.966−0.259）= 0.707
#   ★ 要让交流峰峰 = 1200 Hz（PC 基线同款）⇒ A = 1200/0.707 = **1697 Hz**
#
# 接口（与 PC 侧 `stair6` 严格同表同段长）
#   wire[11] = 峰值频率 A (Hz)      wire[10] = 使能 (>0.5)
#   wire[56] = 段号   wire[57] = 目标   wire[58] = 实际下发频率   ← 观测
# ★ 部署后必须 `python tools/h723_as5600_bind.py`；再 `0x39 op=19 sub=13 arg=1`
# ══════════════════════════════════════════════════════════════════════════

CONST   K1      = 0.259
CONST   K2      = 0.707
CONST   K3      = 0.966
CONST   lim_ms  = 0.0
CONST   dirfwd  = 0.0

# ── 段号 → 归一化因子（阶梯表；`SEL` 按 G>0.5 取 IN1 ⇒ 链式"逐段抬升"）──
#   ★★★ **`SEQ` 步号 1 起**（固件 `engine.c:474`: `out_wire = step_cur + 1`）
#     ⇒ 6 段的门限是 **1.5/2.5/3.5/4.5/5.5**，不是 0.5/1.5/2.5/3.5/4.5（按 0 起写会恒真 ⇒ 轴不动）
GE      s1      IN=wire[30] THR=1.5
GE      s2      IN=wire[30] THR=2.5
GE      s3      IN=wire[30] THR=3.5
GE      s4      IN=wire[30] THR=4.5
GE      s5      IN=wire[30] THR=5.5
SEL     t0      G=s1 IN0=K1 IN1=K2
SEL     t1      G=s2 IN0=t0 IN1=K3
SEL     t2      G=s3 IN0=t1 IN1=K3
SEL     t3      G=s4 IN0=t2 IN1=K2
SEL     t4      G=s5 IN0=t3 IN1=K1

# ── 因子 × A ⇒ 目标频率；再过使能门 ──
MUL     tgt     FROM wire[11] BY=t4
GE      ena     IN=wire[10] THR=0.5
MUL     hz      FROM tgt BY=ena

# ── 运动请求 ──
OUTPUT  o_hz    TO wire[12] FROM hz
OUTPUT  o_dir   TO wire[13] FROM dirfwd
OUTPUT  o_ena   TO wire[14] FROM wire[10]
OUTPUT  o_lim   TO wire[15] FROM lim_ms

# ── 观测 ──
OUTPUT  w_seg   TO wire[56] FROM wire[30]
OUTPUT  w_tgt   TO wire[57] FROM tgt
OUTPUT  w_hz    TO wire[58] FROM hz

# ── 顺序域（**必须文件末尾**）：6 段 × 833ms = 4998ms ≈ **5.0 s 周期 ⇒ f = 0.2 Hz** ──
SEQ     prof    TO wire[30] PERIOD=10ms
  DWELL 833ms
  DWELL 833ms
  DWELL 833ms
  DWELL 833ms
  DWELL 833ms
  DWELL 833ms
  LOOP
