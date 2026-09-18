#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-Q — **“秒”语义的端到端行为验证**（声明的秒 vs 跑出来的秒）

## 为什么必须做这一条
2026-09-18 修了一个**静默缺陷**: `DT_SLOW` 手写成 `0.01f`(10ms), 而 div2 的实际
扫描周期是 **64 拍 = 6.4ms** ⇒ 跑在 div2 上的 `TIMER/LPF/RATE/PID` 拿到的 dt 偏大 56%。
修法是让 `DT_SLOW` 从 (相位模数 × 拍长) **算出来**, 并加编译期断言。

★ 但那次只验了 **编译期断言** 与 **扫描周期**(E-J) ——
  **没有任何一条判据验过“声明的时间常数真的实现出来了”**。
  一个只被断言守着的修复, 与“设了就算”只差一层纸。

## 判据的形态: 只问“声明的物理量实现了吗”, **全篇不提 dt**
| 声明 | 判据 | 载体 |
|---|---|---|
| `LPF τ=2.0s` | 实测时间常数必须是 2.0s | ln(1−y) 对 tick 回归 |
| `PID Ki=1.0/s` | 实测积分斜率必须是 1.0/s (= Ki×err) | 输出对 tick 回归 |
| `TIMER PT=3/5/7s` | 翻转时刻 = 声明秒 | tick 计数 |
| `SEQ 超时 PT=2/4/6s` | 同上 | tick 计数 |
| `RATE/PID-D`（÷dt 族） | 对已知单位阶跃必须报 **1/dt 的整数量子** | 强制源 + 峰值 |

这样判据与实现细节**解耦**: 哪天 dt 又漂了、或者漂到另一个方向, 判据照样失败。

★★ 关键设计: **期望值只从“结构量”导出, 不从 `DT_SLOW` 宏导出**
   `期望 dt ≡ 该档的实际扫描周期 = 相位数 × 拍长`
   —— 这**正是判据的内容**（dt 必须等于实际周期）。
   若从源码里解析 `DT_SLOW` 来当期望, 那么“把 DT_SLOW 改错”这件事会让期望跟着错,
   判据就永远 PASS ⇒ **空判据**。源码里的 `DT_SLOW` 只被**打印出来做对照**, 不参与判定。

## 三个档都在测, 而且 div1 是**对照组**
div1 的 `DT_MID = 0.001f` = 10 拍 × 100µs ✓ 本来就是对的。
⇒ 它必须在**任何**构建下都 PASS。若它跟着 FAIL, 说明测的不是 dt, 而是别的东西。

## 两种「÷dt」怎么观测（逐拍脉冲问题）
`RATE`/`PID-D` 的输出只在“源发生变化后的**那一次**执行”上非零, 宽度 = 一个扫描周期
⇒ div2 上是 6.4ms。串口逐字读~1ms 级 ⇒ 用**强制源(0x24)**反复做一个单位阶跃,
取**多次尝试的峰值**（整数量子 = 1/dt, 相位无关, 只取 max 就收敛到真值）。
  ★ div0 的脉冲宽度 = 1 拍 = 100µs < 单字读耗时 ⇒ **原理上不可被本通道采样**,
    如实记 SKIP（不是 PASS）。div0 的 dt 由 TIMER/LPF/PID 三条慢判据覆盖。

## 判据清单（每条都能失败）
  Q0a  时间轴: tick 速率 = 1/TICK_PERIOD_US（对照**上位机墙钟**）—— 这是"tick=100µs"
       从假设变成**观测**的那一步; 掉拍/复位都会让它失败
  Q0a″ 环写增量 == tick 增量（RUN 段 1:1）
  Q0b  两个强制源: 写 5.0 回读 == 5.0（0x24 真的生效 —— ÷dt 判据的唯一输入）
  Q3 ★ **绝对秒主判据**: PID 积分斜率 = Ki×err /s（三档各一条）
       —— 200 个连续样本上的回归 ⇒ 实测精度 0.01~0.08%
  Q1b  TIMER（路由域）div2 与 div0 **同拍配对** |差| ≤200 tick（免拟合, 免偏斜）
  Q4b  SEQ（顺序域） 同上
  Q-A′ 六点合并拟合（跨域）残差 rms ≤600 tick —— 问"两个域是不是同一个秒"
  Q2   LPF 三档实测 τ 都在声明值 ±3%
  Q5   RATE/PID-D 的峰值被**锁存梯**夹在区间里（免竞态; div2 两条 + div1 对照一条）
  Q5-次级 轮询峰值直接读数值（命中不足则 SKIP —— 读一次串口 17ms > 脉冲 6.4ms）

## 不判的（写明理由, **不是 PASS**）
  · RATE/PID-D **div0** —— 脉冲宽 1 拍, 同拍锁存器读到的永远是 0（结构性不可测）
  · TIMER/SEQ 的 div0 **三点拟合斜率** —— 翻转检测粒度 ≈480 拍 ⇒ 斜率噪声 ≈0.5%,
    与容差同量级 ⇒ 会随机 FAIL。**绝对秒改用 Q3**; 三点拟合只打印作描述。

用法
  python tools/exp_eq_dt_semantics.py            # 全跑（约 25 s）
  python tools/exp_eq_dt_semantics.py --json out.json
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足或覆盖不足（判无效）
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, json, math, os, re, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

# ── 协议 ──────────────────────────────────────────────────────────────
CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_BURST = 0x38, 0x10, 0x12, 0x11, 0x22
CMD_SEQ_DEPLOY, CMD_FORCE = 0x44, 0x24

# ── SHM 偏移（engine.h）────────────────────────────────────────────────
OFF_WIRE_MAP      = 0x0240
OFF_ENG_TICKS     = 0x3820
OFF_EXEC_RING_HDR = 0x3880
# ★★ 时间轴用**环头里的 tick**（`main.c:1488`, **ISR 直接写**）, 不用 `OFF_ENG_TICKS`:
#   `OFF_ENG_TICKS` 是主循环镜像（`main.c:4948`）—— 本工具会连续做大块 0x22 读,
#   而那段读正是在主循环里服务的 ⇒ 镜像可能滞后**整整一个主循环周期**（~15ms = 150 拍）,
#   于是"读到的时间"与"读到的 wire 值"不同刻, 翻转时刻的判据会被这层滞后污染。
#   环头 [0]=写计数 [1]=g_tick_count 都由 ISR 每拍写 ⇒ 无滞后。
OFF_RING_W, OFF_RING_TICK = 0, 4

# ── 原语 / 源 / 目的 ──────────────────────────────────────────────────
OP_LPF, OP_PID, OP_RATE, OP_TIMER, OP_HYST = 0x04, 0x05, 0x06, 0x0C, 0x02
SRC_WIRE, SRC_CONST, DST_WIRE = 1, 2, 2
FLAG_ACTIVE = 0x01
DIV_FAST, DIV_MID, DIV_SLOW = 0, 1, 2

W_FORCE = 30          # 强制源 wire（表内**无生产者** ⇒ 0x10 的欠采样门不适用）
# ★★ 两个独立强制源, 因为 RATE 与 PID-D 对**源斜率的要求相反**:
#     RATE   = (src−prev)/dt     ⇒ 要 src **递增** 才得正值
#     PID-D  = Kd·(err−prev)/dt, err = sp−src ⇒ **只由 d(src)/dt 定号**（与 sp 无关）
#              ⇒ 要 src **递减** 才得正值（递增会得到 −78.1, 被 PID 输出的 [0,100] 夹成 0）
#   第一版只用一个递增源 ⇒ PID-D 恒为 0, 而"锁存梯全 0"看起来像"没锁上"。
#   两个源都**无生产者** ⇒ host 写入即长期有效（也不会被任何路由覆写）。
W_FORCE2 = 31
SEQ_W0  = 12          # seq 步号镜像 wire 起点 —— ★ 紧接 TIMER 之后, 使**慢量一次读完**

# ★★ wire 布局是**判据的一部分**, 不是排版: 串口读一次要 ~0.65ms/字,
#   而"翻转时刻"的测量分辨率 = 一次采样的耗时 ⇒ 读得越短, 判据越锐。
#   wire[0..17]  = 慢量（一次 18 字读完）
#   wire[21..25] = 脉冲量（÷dt 族, 一次 5 字读完; div2 的两个相邻）
TICK_HEALTHY = 200000
SEQ_PT_DIV0  = ((2.0, 26), (4.0, 27), (6.0, 28))    # (声明秒, param_idx)
SEQ_PT_DIV2  = ((2.0, 26), (4.0, 27), (6.0, 28))
TIMER_PT     = (3.0, 5.0, 7.0)


# ══════════════════════════════════════════════════════════════════════
# 源码量: **结构量**参与判定, `DT_SLOW` 只打印
# ══════════════════════════════════════════════════════════════════════
def read_src():
    path = os.path.join(ROOT, "src", "engine.h")
    txt = open(path, encoding="utf-8", errors="replace").read()

    def cint(name):
        m = re.search(r"^#define\s+%s\s+(\d+)u?\b" % re.escape(name), txt, re.M)
        if not m:
            raise SystemExit("!! 源码里找不到 `#define %s`（engine.h 变了? 判据的期望值来源断了）" % name)
        return int(m.group(1))

    out = dict(
        tick_us     = cint("TICK_PERIOD_US"),
        ph1         = cint("BUCKET_DIV1_PHASES"),
        ph2_used    = cint("BUCKET_DIV2_PHASES_USED"),
        ph2_table   = cint("BUCKET_DIV2_PHASES"),
        max_routes  = cint("MAX_ROUTES"),
    )
    m = re.search(r"^#define\s+DT_SLOW\s+(.+?)\s*(?:/\*.*)?$", txt, re.M)
    out["dt_slow_text"] = m.group(1).strip() if m else "?"
    # ★ 判定用的期望 dt = **结构量**（实际扫描周期），不是宏
    out["dt"] = {DIV_FAST: out["tick_us"] / 1e6,
                 DIV_MID:  out["ph1"] * out["tick_us"] / 1e6,
                 DIV_SLOW: out["ph2_used"] * out["tick_us"] / 1e6}
    out["tick_s"] = out["tick_us"] / 1e6
    out["hz"] = 1.0 / out["tick_s"]
    return out


# ══════════════════════════════════════════════════════════════════════
# 程序构造
# ══════════════════════════════════════════════════════════════════════
# (op, div, src_type, src_index, dst, param_idx, state_off) —— 顺序即表序
ROUTES = [
    # —— 慢量: LPF / PID 积分 / TIMER（三档各一份）——
    (OP_LPF,   DIV_FAST, SRC_CONST, 3,  0,  0,  1),
    (OP_LPF,   DIV_SLOW, SRC_CONST, 3,  1,  1,  2),
    (OP_LPF,   DIV_MID,  SRC_CONST, 3,  2,  2,  3),
    (OP_PID,   DIV_FAST, SRC_CONST, 4,  3,  5,  4),
    (OP_PID,   DIV_SLOW, SRC_CONST, 4,  4,  6,  5),
    (OP_PID,   DIV_MID,  SRC_CONST, 4,  5,  7,  6),
    (OP_TIMER, DIV_FAST, SRC_CONST, 3,  6,  8,  7),
    (OP_TIMER, DIV_SLOW, SRC_CONST, 3,  7,  9,  8),
    (OP_TIMER, DIV_FAST, SRC_CONST, 3,  8, 10,  9),
    (OP_TIMER, DIV_SLOW, SRC_CONST, 3,  9, 11, 10),
    (OP_TIMER, DIV_FAST, SRC_CONST, 3, 10, 12, 11),
    (OP_TIMER, DIV_SLOW, SRC_CONST, 3, 11, 13, 12),
    # —— 脉冲量: RATE / PID-D（÷dt 族）；div2 的两个**相邻存放**（22,23）便于快读 ——
    (OP_RATE,  DIV_FAST, SRC_WIRE, W_FORCE, 20, 20, 13),
    (OP_RATE,  DIV_MID,  SRC_WIRE, W_FORCE, 21, 21, 14),
    (OP_RATE,  DIV_SLOW, SRC_WIRE, W_FORCE, 22, 22, 15),
    # ★ PID-D 只留 div2: div0 的脉冲 100µs 不可采样; div1 的 +0.5/0.001 = 500 会撞
    #   PID 输出的 [0,100] 上限 ⇒ 测到的是**钳位值**不是 dt（判据会假 FAIL）。
    #   div1 的 ÷dt 由同一常量族的 LPF/PID-I div1 两条精确判据覆盖。
    (OP_PID,   DIV_SLOW, SRC_WIRE, W_FORCE2, 23, 25, 18),
]

# ══════════════════════════════════════════════════════════════════════
# ★★★ 锁存梯（÷dt 族的**免竞态**观测）—— 本实验最关键的一处设计
#
# 问题: RATE/PID-D 的输出只在"源变化后的那一次执行"上非零, 宽度 = 一个扫描周期
#       （div2 = 6.4ms）。而上位机读一次串口要 **17ms**（见下方实测）⇒
#       靠轮询"抓脉冲"的命中率 = 6.4/(17+…) ≈ 20~35%, 而且相位不可控 ——
#       实测第一版 **0/200 命中**（读之前还睡了 15ms ⇒ 快照永远落在脉冲之后）。
#
# 解法: 把瞬态**变成永久状态** —— 用 HYST 当锁存器:
#       `prim_hyst`: state=0 时 src>value_a ⇒ state=1; state=1 时 src<value_b ⇒ state=0。
#       取 **value_b = 0** ⇒ `src < 0` 恒假 ⇒ **置位后永不复位** = 一次性锁存 ✓
#       它同时还是比较器 ⇒ **一个阈值 = 一条路由**, 不需要 CMP 串联。
#
# ★ 为什么它免竞态: 锁存路由是 **div0**（每拍都跑）⇒ 脉冲在 wire 上留 64 拍,
#   锁存器必然在其中某一拍看到它。上位机什么时候读、读多慢都不影响结果。
# ★ 阈值的排布使结论**双向可失败**（这是"能失败"的正形）:
#       正确 dt ⇒ RATE div2 峰值 156.25 ⇒ 超过 145 但不超 165
#       陈旧 dt ⇒              峰值 100    ⇒ 一个都不超
#   两个方向的读数都必须是"对的"才算 PASS —— 只判"超了 145"无法排除"超得多得多"。
LATCH = [
    # (源 wire, 阈值, 锁存 wire) —— 22 = RATE div2, 23 = PID-D div2, 21 = RATE div1
    (22, 105.0, 40), (22, 125.0, 41), (22, 145.0, 42), (22, 165.0, 43),
    (23,  55.0, 44), (23,  85.0, 45),
    (21, 900.0, 46), (21, 1100.0, 47),   # div1 对照: 脉冲宽仅 1ms, 靠锁存照样抓得住
]
# ★ 每个源上的阈值**成对跨过期望值**, 于是图案给出一个**区间**而不是一个布尔:
#     超了低阈值 = 峰值确实到了; 没超高阈值 = 峰值没有失控。
#   只判前者会漏掉"超得多得多", 只判后者会漏掉"根本没动" —— 两个方向都要对。
LATCH_EXPECT = {22: (145.0, 165.0), 23: (55.0, 85.0), 21: (900.0, 1100.0)}
# ★ HYST 是**有状态**原语 ⇒ 每条锁存路由必须有**独立且非零**的 state_offset
#   （共用状态 ⇒ 六个阈值互相置位, 而症状是"锁存图案看起来也像数据"）。
#   param_idx = 29+k（阈值 T 存 value_a）, state_offset = 19+k。
for _k, (_si, _thr, _dst) in enumerate(LATCH):
    ROUTES.append((OP_HYST, DIV_FAST, SRC_WIRE, _si, _dst, 29 + _k, 19 + _k))

NPARAMS = 29 + len(LATCH)
NSTATES = 19 + len(LATCH)


def params_blob():
    """param 表（每格 4×f32）。★ 索引是**契约**: 见 ROUTES 的 param_idx 与注释。"""
    p = [None] * NPARAMS
    for i in range(NPARAMS):
        p[i] = (0.0, 0.0, 0.0, 0.0)
    p[0] = p[1] = p[2] = (2.0, 0.0, 0.0, 0.0)      # LPF τ=2.0s（三档同值）
    p[3] = (1.0, 0.0, 0.0, 0.0)                    # CONST 1.0（阶跃/计时输入）
    p[4] = (0.0, 0.0, 0.0, 0.0)                    # CONST 0.0（PID 积分的 src）
    p[5] = p[6] = p[7] = (0.0, 1.0, 0.0, 1.0)      # PID 积分: Kp=0 Ki=1 Kd=0 sp=1 ⇒ 输出 = t
    for k, pt in enumerate(TIMER_PT):              # TIMER: value_a=PT, value_b=mode=0
        p[8 + 2 * k] = (pt, 0.0, 0.0, 0.0)
        p[9 + 2 * k] = (pt, 0.0, 0.0, 0.0)
    p[20] = p[21] = p[22] = (0.0, 0.0, 0.0, 0.0)   # RATE 不用参数
    p[23] = p[24] = p[25] = (0.0, 0.0, 0.5, 0.0)   # PID-D: Kp=0 Ki=0 Kd=0.5 sp=0
    p[26] = (0.0, 2.0, 0.0, 0.0)                   # SEQ 超时秒 = value_b
    p[27] = (0.0, 4.0, 0.0, 0.0)
    p[28] = (0.0, 6.0, 0.0, 0.0)
    for k, (_si, thr, _dst) in enumerate(LATCH):
        # HYST: value_a = 置位阈值, value_b = 复位阈值 = 0 ⇒ **永不复位**的一次性锁存
        p[29 + k] = (thr, 0.0, 0.0, 0.0)
    return b"".join(struct.pack("<4f", *q) for q in p)


def routes_blob():
    out = []
    for (op, div, st, si, dst, pidx, soff) in ROUTES:
        # period = div | phase<<2：**div2/div1 一律 phase 0**
        #   ⇒ 同档的所有路由在**同一拍**执行 ⇒ 档内零相位偏斜（判据不需要补这个偏斜）
        period = div | (0 << 2)
        # ★★ 四个 u16 的**内存序** = (param_idx, state_offset, actuator_idx, wire2_idx)
        #    —— 不是我在 ROUTES 里写的顺序, 也不是"看起来合理"的顺序。
        #    ★ 第一版按 (wire2_idx, param_idx, state_offset, actuator_idx) 传, 结果
        #      state_offset 收到的是 param_idx(=0) ⇒ 固件 NAK
        #      "stateful op needs state_offset"。
        #    ★★ 而且**我自己的离线对拍脚本没能抓到**: 它按同一份错模型解包,
        #      于是"写进去的"和"读出来的"完全自洽 —— 与 §5.67 载荷字段错位同族。
        #      教训: 上位机的自检与写入**共用同一份模型** ⇒ 结构错位只有**对端的
        #      校验器**能发现。别把"离线自检通过"当成"格式对"。
        out.append(struct.pack("<BBBBBBHHHHBB", st, si, DST_WIRE, dst, op,
                               FLAG_ACTIVE, pidx, soff, 0, 0, period, 0))
    return b"".join(out)


def deploy_payload():
    nr = len(ROUTES)
    # ★ 载荷 = [nr:u16][np:u16][ns:u16] + 路由×nr + 参数×np + 状态×ns（固件: need = 6+(nr+np+ns)*16）
    #   一行都不多 —— 多余字节虽然被 `n >= need` 放过, 但"载荷里有点没人读的东西"
    #   正是下次改格式时踩错位置的温床。
    return (struct.pack("<HHH", nr, NPARAMS, NSTATES) + routes_blob()
            + params_blob() + b"\x00" * (16 * NSTATES))


def seq_payload():
    """0x44: [n_seq:u8][n_steps:u16] + 目录(n_seq×6B) + 步表(n_steps×16B)

    实例 0..2 = div0（PT=2/4/6）, 实例 3..5 = div2（PT=2/4/6）。
    period 字节只给 div —— **相位由固件分配**（h_seq_deploy: phase = 同 div 组内序号）。
    每实例 2 步, 两步都是「无条件 + 使能超时」（cond_type=2 必须配 timeout_en, 否则 NAK）。
    步 1 是末步 ⇒ 反复"推进到自己", out_wire 恒 2.0（单调, 便于判翻转）。
    """
    insts = [(DIV_FAST, 26), (DIV_FAST, 27), (DIV_FAST, 28),
             (DIV_SLOW, 26), (DIV_SLOW, 27), (DIV_SLOW, 28)]
    dirs, tbl = [], []
    off = 0
    for i, (div, pidx) in enumerate(insts):
        dirs.append(struct.pack("<BBBBH", 2, SEQ_W0 + i, div, 0, off))
        for _ in range(2):
            # SeqStepEntry_t: cond_type u8, cond_idx u8, flags u8, rsvd u8,
            #                 param_idx u16, state_offset u16, jump_idx u16, rsvd2 u32
            # ★★ 尾部**必须补 2 字节**: 结构体是 `packed, aligned(4)`, 字段和 = 14,
            #    而 `sizeof == 16` 由 `_Static_assert` 守着（对齐补齐 2 字节）。
            #    ★ 这一条是**离线对拍**抓到的: 我第一版按字段和写了 14 字节/步,
            #      12 步就少 24 字节 ⇒ 固件会 NAK "seq: length mismatch"。
            #      手写打包结构与 C 结构体不一致, 是本项目的老族（§5.67 载荷字段错位）。
            tbl.append(struct.pack("<BBBBHHHI", 2, 0, 0x02, 0, pidx, 0, 0, 0) + b"\x00\x00")
        off += 2
    return (struct.pack("<BH", len(insts), off) + b"".join(dirs) + b"".join(tbl))


# ══════════════════════════════════════════════════════════════════════
# 串口小工具
# ══════════════════════════════════════════════════════════════════════
def rd(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(CMD_BURST, struct.pack("<IH", addr + 4 * off, k),
                          expect_len=4 * k)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def ring_head(dcl, shm):
    """→ (写计数, g_tick_count) —— 两者都由 **ISR 每拍写**, 无主循环镜像滞后。

    ★ 读 **2 个字**(8B) 而不是 1 个: 一是协议层纪律（4B 应答与 `0x01 GET_VERSION`
      的应答**完全同长**, 长度判据在那一档救不了 —— 见 `h723_client.send` 的 docstring）,
      二是顺带拿到写计数, 可与 tick 增量互校（跑拍数是否= tick 增量）。
    """
    raw = rd(dcl, shm + OFF_EXEC_RING_HDR, 2)
    if not raw or len(raw) < 8:
        return None
    return struct.unpack("<2I", raw[:8])


def tick_now(dcl, shm):
    h = ring_head(dcl, shm)
    return h[1] if h else None


def _wait_healthy(dcl, shm, tries=6, need=3):
    """板子在复位—恢复态时所有量都不可信 —— 见 h723_tick_ring.py 的同名函数（现场证据）。"""
    for i in range(tries):
        t0, good = time.time(), 0
        while time.time() - t0 < 12.0:
            v = ring_head(dcl, shm)
            if v and v[1] >= TICK_HEALTHY:
                good += 1
                if good >= need:
                    print("    [健康门] 第 %d 次: tick=%d 环写=%d ⇒ 开工（%.1f s）"
                          % (i + 1, v[1], v[0], time.time() - t0))
                    return True
            else:
                good = 0
            time.sleep(0.25)
        print("    [健康门] 第 %d 次: 12 s 内 tick 始终 < %d ⇒ 复位重来" % (i + 1, TICK_HEALTHY))
        try:
            dcl.close()
        except Exception:
            pass
        time.sleep(0.4)
        dcl.__init__(dcl.port, wait=1.0)
    return False


# ══════════════════════════════════════════════════════════════════════
# 拟合
# ══════════════════════════════════════════════════════════════════════
def lin(xs, ys):
    """→ (b, a, rms)：y ≈ a + b·x"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my, 0.0
    b = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    a = my - b * mx
    res = [ys[i] - (a + b * xs[i]) for i in range(n)]
    return b, a, (sum(r * r for r in res) / n) ** 0.5


def first_cross(rec, idx, thr=0.5):
    """首个 ≥thr 的 tick; 并检查此后不再回落（单调性由调用方报告）。"""
    for r in rec:
        if r[1][idx] >= thr:
            return r[0]
    return None


def never_returns(rec, idx, tk_cross, thr=0.5):
    return all(r[1][idx] >= thr for r in rec if r[0] >= tk_cross)


# ══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--seconds", type=float, default=10.5, help="慢量观测窗（s）")
    ap.add_argument("--flips", type=int, default=150, help="源阶梯步数（÷dt 族）")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    S = read_src()
    dt = S["dt"]
    print("=" * 74)
    print("E-Q  “秒”语义端到端验证   ——   声明的秒 vs 跑出来的秒")
    print("=" * 74)
    print("源码量（src/engine.h）:")
    print("    TICK_PERIOD_US            = %d µs" % S["tick_us"])
    print("    BUCKET_DIV1_PHASES        = %d" % S["ph1"])
    print("    BUCKET_DIV2_PHASES_USED   = %d   (桶表尺寸 %d)"
          % (S["ph2_used"], S["ph2_table"]))
    print("    DT_SLOW 源码文本          = `%s`   ← **只对照, 不参与判定**"
          % S["dt_slow_text"])
    print()
    print("★ 判定用的期望 dt = **结构量**（该档实际扫描周期 = 相位数 × 拍长）:")
    print("    div0: %d 拍 × %g µs = %.6f s" % (1, S["tick_us"], dt[DIV_FAST]))
    print("    div1: %d 拍 × %g µs = %.6f s" % (S["ph1"], S["tick_us"], dt[DIV_MID]))
    print("    div2: %d 拍 × %g µs = %.6f s" % (S["ph2_used"], S["tick_us"], dt[DIV_SLOW]))
    print("    ⇒ 期望 tick 速率 = %.1f Hz" % S["hz"])
    print("    ⇒ 为什么这样取: 判据就是「dt 必须等于实际周期」。若从源码解析 DT_SLOW 当期望,")
    print("      那么把 DT_SLOW 改错会让期望跟着错 ⇒ 永远 PASS = 空判据。")
    print()
    exp = dict(rate=1.0 / dt[DIV_SLOW], rate1=1.0 / dt[DIV_MID],
               q_rate=1.0 / dt[DIV_SLOW], q_pid=0.5 / dt[DIV_SLOW],
               q_rate1=1.0 / dt[DIV_MID], q_pid1=0.5 / dt[DIV_MID])
    print("预期可观测量:")
    print("    TIMER/SEQ 斜率       = %.1f tick/声明秒" % S["hz"])
    print("    LPF τ_eff            = 2.000 s（离散化 +dt/2 = +%.4f s, 在容差内）"
          % (dt[DIV_SLOW] / 2))
    print("    PID 积分斜率         = 1.000 /s（Ki×err）")
    print("    RATE div2 峰值       = 1/dt2   = %.2f" % exp["q_rate"])
    print("    PID-D div2 峰值      = Kd/dt2  = %.2f   (Kd=0.5)" % exp["q_pid"])
    print("    RATE/PID-D div1 峰值 = %.1f / %.1f（次级证据, 脉冲宽 1ms）"
          % (exp["q_rate1"], exp["q_pid1"]))
    print()

    dcl = Dcl(args.port)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)
    res = []          # (判据, bool)
    skip = []         # 覆盖不足的条目（**不是 PASS**）
    data = {}

    try:
        sts, p = dcl.send(CMD_STATUS, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)
        if not _wait_healthy(dcl, shm):
            print("\n[exit] 板子不健康 ⇒ **判无效**（不测一个正在复位的板子）")
            return 2

        # ── 部署: STOP → 0x10 → 0x44 → force → START ──────────────────
        # ★ 先 STOP 再部署: 否则 0x44 会被 NAK("stop engine first"), 而且
        #   **计时器会在 deploy 与 START 之间偷偷累计**, 让 t0 不再是公共起点。
        # ★ deploy/seq 一律 `expect_len=None` 再看长度: `Dcl.send` 的 expect_len 语义是
        #   "长度不符就重发", 而 **NAK 的载荷是错误串**(长度必然不符) ⇒ 会把一句
        #   可读的拒绝理由吞掉、只剩 "TIMEOUT"。诊断价值比省一次重试大得多。
        print("\n── 部署 ──")
        dcl.send(CMD_STOP); time.sleep(0.15)
        sts, pp = dcl.send(CMD_DEPLOY, deploy_payload(), expect_len=None)
        if sts != "ACK":
            print("!! 0x10 deploy 被拒: %s" % (pp.decode('utf-8', 'replace') if sts == 'NAK' else sts))
            return 2
        if len(pp) != 6:
            print("!! 0x10 应答长度 %d ≠ 6（杂帧?）⇒ 判无效" % len(pp)); return 2
        print("    0x10 路由表: %d 条 ACK budget=%d" % (len(ROUTES), struct.unpack("<HI", pp[:6])[1]))
        sts, pp = dcl.send(CMD_SEQ_DEPLOY, seq_payload(), expect_len=None)
        if sts != "ACK":
            print("!! 0x44 seq deploy 被拒: %s" % (pp.decode('utf-8', 'replace') if sts == 'NAK' else sts))
            return 2
        print("    0x44 顺序表: 6 实例 × 2 步 ACK")

        # Q0b 前置: 两个强制源真的生效吗（0x24 是后面 ÷dt 判据的**唯一输入**）
        #   ★ 读 2 字而非 1 字: 1 字的应答(4B)与 `0x01 GET_VERSION` 的应答**同长**,
        #     协议层没有命令码回显 ⇒ 那一档的长度判据救不了（见 h723_client.send）。
        for wi in (W_FORCE, W_FORCE2):
            dcl.send(CMD_FORCE, struct.pack("<HBf", wi, 1, 5.0)); time.sleep(0.05)
            rb = rd(dcl, shm + OFF_WIRE_MAP + 4 * wi, 2)
            v_force = struct.unpack("<f", rb[:4])[0] if rb else float("nan")
            res.append(("Q0b 强制源 wire[%d] 生效: 写 5.0 回读 == 5.0（读回 %.3f）" % (wi, v_force),
                        abs(v_force - 5.0) < 1e-5))
            print("    Q0b 强制源 wire[%d]: 写 5.0 回读 %.3f" % (wi, v_force))
            dcl.send(CMD_FORCE, struct.pack("<HBf", wi, 1, 0.0)); time.sleep(0.05)

        # ── 慢量观测窗 ────────────────────────────────────────────────
        print("\n── 阶段 A: 慢量（LPF/PID/TIMER/SEQ）, 观测 %.1f s ──" % args.seconds)
        dcl.send(CMD_START)
        t_wall0 = time.time()
        h0 = ring_head(dcl, shm)
        if h0 is None:
            print("!! 读环头失败 ⇒ 判无效"); return 2
        tk0, w0 = h0[1], h0[0]
        rec = []                     # (tick, wire[0..17], 相对墙钟, 环写计数)
        NW = 18                      # ★ 一次读完: 0..2 LPF · 3..5 PID · 6..11 TIMER · 12..17 SEQ
        while True:
            tw = time.time()
            raw = rd(dcl, shm + OFF_WIRE_MAP, NW)
            h = ring_head(dcl, shm)
            if raw is None or h is None:
                print("!! 读失败 ⇒ 判无效"); return 2
            ws = struct.unpack("<%df" % NW, raw[:4 * NW])
            rec.append((h[1], ws, tw - t_wall0, h[0]))
            if tw - t_wall0 >= args.seconds:
                break
        print("    样本 %d 个, tick %d → %d（跨度 %.3f s）, 环写 %d → %d"
              % (len(rec), rec[0][0], rec[-1][0], rec[-1][2] - rec[0][2], w0, rec[-1][3]))
        print("    ★ 采样周期 ≈ %.1f ms ⇒ 翻转时刻的分辨率 ≈ %d 拍（判据容差 ±600 拍的来源）"
              % (1000.0 * (rec[-1][2] - rec[0][2]) / max(1, len(rec) - 1),
                 int(1000.0 * (rec[-1][2] - rec[0][2]) / max(1, len(rec) - 1) / (S["tick_us"] / 1000.0))))

        # Q0a 时间轴: tick 对**墙钟**回归 —— tick=100µs 是判据的前提, 必须实测
        b_t, a_t, rms_t = lin([r[2] for r in rec], [r[0] for r in rec])
        print("\n── Q0a 时间轴 ──")
        print("    实测 tick 速率 = %.3f Hz（%d 点, 残差 rms %.2f tick）"
              % (b_t, len(rec), rms_t))
        print("    期望           = %.1f Hz（1/TICK_PERIOD_US）" % S["hz"])
        print("    相对偏差       = %+.3f%%" % (100.0 * (b_t / S["hz"] - 1.0)))
        # ★ 读的顺序是「先 wire 块, 后环头」⇒ tick 比 wire 快照晚 ~0.5ms(常数偏斜)。
        #   常数偏斜在**斜率**里抵消, 在 div0/div2 配对里也抵消（两侧同偏）。
        print("    ★ 读数顺序 = 先 wire 块后环头 ⇒ tick 相对 wire 有常数偏斜(~5 拍);")
        print("      斜率与 div0/div2 配对都把它抵消掉。")
        res.append(("Q0a tick 速率 = %.1f Hz ±0.3%%（实测 %.3f）" % (S["hz"], b_t),
                    abs(b_t / S["hz"] - 1.0) <= 0.003))
        d_w = rec[-1][3] - w0
        d_t = rec[-1][0] - tk0
        print("    交叉核对: 环写增量 %d, tick 增量 %d ⇒ 差 %+d（RUN 段应 1:1）"
              % (d_w, d_t, d_w - d_t))
        res.append(("Q0a″ 环写增量 == tick 增量（RUN 段 1:1, 差 %+d）" % (d_w - d_t),
                    abs(d_w - d_t) <= 2))
        mono = all(rec[i][0] <= rec[i + 1][0] for i in range(len(rec) - 1))
        res.append(("Q0a′ tick 单调不回卷（复位/掉拍会打破）", mono))
        data["tick_hz"] = b_t

        # ── Q1/Q4: TIMER / SEQ 翻转时刻 ───────────────────────────────
        print("\n── Q1 TIMER（声明 PT 秒 → 翻转 tick）──")
        tim = []
        for k, pt in enumerate(TIMER_PT):
            tim.append((pt, 6 + 2 * k, 7 + 2 * k))
        print("    %-8s %-14s %-14s %s" % ("PT(s)", "div0 翻转tick", "div2 翻转tick", "div2−div0"))
        t0s, t2s = [], []
        for pt, wi0, wi2 in tim:
            c0, c2 = first_cross(rec, wi0), first_cross(rec, wi2)
            t0s.append(c0); t2s.append(c2)
            print("    %-8.1f %-14s %-14s %s"
                  % (pt, c0, c2, ("%+d" % (c2 - c0)) if (c0 and c2) else "?"))
            if c0 is None or c2 is None:
                res.append(("Q1 TIMER PT=%.0fs: 两条都翻转过 (wire %d/%d)" % (pt, wi0, wi2), False))
            else:
                res.append(("Q1 TIMER PT=%.0fs: 翻转后不回落（无线重启）" % pt,
                            never_returns(rec, wi0, c0, 0.5) and never_returns(rec, wi2, c2, 0.5)))
        if all(t0s) and all(t2s):
            b, c1, rms = lin(list(TIMER_PT), t0s)
            print("    div0 三点拟合: tick = %.1f + %.4f × PT   (残差 rms %.1f tick)" % (c1, b, rms))
            print("    ⇒ 实测每声明秒 = %.4f tick（期望 %.1f）⇒ 相对偏差 %+.3f%%"
                  % (b, S["hz"], 100.0 * (b / S["hz"] - 1.0)))
            print("    ★ 这一条**不作为判据**（3 点拟合的斜率噪声 ≈0.3%, 比容差还大 ——")
            print("      四次运行的实测值: +0.152%% / −0.165%% / −0.262%% / −0.033%%。")
            print("      绝对秒判据改用下面 Q-A 的 **6 点合并拟合**（跨越路由域与顺序域）。")
            # ★★ Q1b 用**同拍配对**而不是"套 div0 三点直线外推":
            #   div0/div2 两条 wire 在**同一次快照**里读 ⇒ 检测误差是同一个"落在哪个采样
            #   区间"; 两者真相差 ≤64 拍时, 检测差最多 = 64 拍 + 跨区间 1 次采样。
            #   实测三轮全部 **+0/+0/+0**, 而"若是陈旧 dt"应为 −10800/−18000/−25200。
            worst, ok = 0, True
            for pt, c0, c2 in zip(TIMER_PT, t0s, t2s):
                dev = c2 - c0
                worst = max(worst, abs(dev))
                print("    PT=%.0fs 配对: div2−div0 = %+d tick"
                      "（若 div2 的 dt 是陈旧的 0.01f, 此处应 ≈ %+.0f）"
                      % (pt, dev, -0.36 * pt * S["hz"]))
                if abs(dev) > 200:
                    ok = False
            res.append(("Q1b TIMER div2 与 div0 **同拍配对** |差| ≤200 tick（最大 %d）" % worst, ok))
            data["timer_pair"] = [c2 - c0 for c0, c2 in zip(t0s, t2s)]
        else:
            skip.append("Q1 TIMER 翻转时刻 —— 观测窗内没抓到翻转, 覆盖不足")

        print("\n── Q4 SEQ（声明超时秒 → 步号翻转 tick）──")
        sq0, sq2 = [], []
        print("    %-8s %-14s %-14s %s" % ("PT(s)", "div0 翻转tick", "div2 翻转tick", "div2−div0"))
        for k, pt in enumerate((2.0, 4.0, 6.0)):
            c0, c2 = first_cross(rec, SEQ_W0 + k, 1.5), first_cross(rec, SEQ_W0 + 3 + k, 1.5)
            sq0.append(c0); sq2.append(c2)
            print("    %-8.1f %-14s %-14s %s"
                  % (pt, c0, c2, ("%+d" % (c2 - c0)) if (c0 and c2) else "?"))
            if c0 is None or c2 is None:
                res.append(("Q4 SEQ PT=%.0fs: 两条都翻转过 (wire %d/%d)"
                            % (pt, SEQ_W0 + k, SEQ_W0 + 3 + k), False))
            else:
                res.append(("Q4 SEQ PT=%.0fs: 步号镜像单调不回退（末步自推进不倒退）" % pt,
                            never_returns(rec, SEQ_W0 + k, c0, 1.5)
                            and never_returns(rec, SEQ_W0 + 3 + k, c2, 1.5)))
        if all(sq0) and all(sq2):
            b2, a2, rms2 = lin([2.0, 4.0, 6.0], sq0)
            print("    div0 三点拟合: tick = %.1f + %.4f × PT   (残差 rms %.1f tick)" % (a2, b2, rms2))
            print("    ⇒ 实测每声明秒 = %.4f tick（期望 %.1f）⇒ 相对偏差 %+.3f%%"
                  % (b2, S["hz"], 100.0 * (b2 / S["hz"] - 1.0)))
            print("    ★ 同样**不单独作判据** —— 与 TIMER 的 div0 点合并见下面 Q-A。")
            # ★★ 与 Q1b 同款: 同拍配对（div0/div2 的两条 wire 在同一次快照里）
            worst2, ok2 = 0, True
            for pt, c0, c2 in zip((2.0, 4.0, 6.0), sq0, sq2):
                dev = c2 - c0
                worst2 = max(worst2, abs(dev))
                print("    PT=%.0fs 配对: div2−div0 = %+d tick（%.1f%%）"
                      "  ★ 若 div2 的 dt 是陈旧的 0.01f, 此处应 ≈ %+.0f tick"
                      % (pt, dev, 100.0 * dev / (b2 * pt), -0.36 * pt * S["hz"]))
                if abs(dev) > 200:
                    ok2 = False
            res.append(("Q4b SEQ div2 与 div0 **同拍配对** |差| ≤200 tick（最大 %d）" % worst2, ok2))
            data["seq_pair"] = [c2 - c0 for c0, c2 in zip(sq0, sq2)]
        else:
            skip.append("Q4 SEQ 翻转时刻 —— 没抓到翻转, 覆盖不足")

        # ── Q-A 绝对秒: 合并**两个域**的 6 个 div0 点做一次拟合（**只作描述**）────
        # ★★ 为什么它**不当判据**（这是本次实验的一处方法学收获）:
        #   "翻转时刻"的检测粒度 = 一个采样周期（实测 ~480 拍）。6 个点各自独立地晚
        #   0~480 拍 ⇒ 拟合斜率的噪声 ≈ rms/√Sxx ≈ 220/4.2 ≈ **53 tick/s = 0.53%**,
        #   比 ±0.5% 的容差还大 —— 那样的判据会随机 FAIL（实测四轮 +0.28%/−0.03%/…）。
        #   ⇒ **绝对秒的精确判据交给 Q3（PID 积分斜率）**: 它是 200 个连续样本上的回归,
        #     而不是 6 个阶跃点的差分, 实测精度 **0.01~0.08%**（高一个数量级）。
        #   这里保留它: ① 打印"每声明秒 = 多少 tick"作为跨域一致性的**描述**;
        #                 ② 判据只判**残差 rms**（= 两个域是否用同一个秒）。
        if all(t0s) and all(sq0):
            xs = list(TIMER_PT) + [2.0, 4.0, 6.0]
            ys = list(t0s) + list(sq0)
            bb, cc, rr = lin(xs, ys)
            print("\n── Q-A 跨域一致性（div0 六点合并: 3×TIMER + 3×SEQ）──")
            print("    tick = %.1f + %.4f × 声明秒   残差 rms %.1f tick" % (cc, bb, rr))
            print("    ⇒ 每声明秒 = %.2f tick（期望 %.1f）· 相对 %+.3f%%"
                  " —— **描述, 不作判据**（斜率噪声 ≈0.5%% > 容差; 精确值看 Q3）"
                  % (bb, S["hz"], 100.0 * (bb / S["hz"] - 1.0)))
            print("    ⇒ 判据: 残差 rms ≤600 tick（≈ 一个采样周期的量级）—— 它问的是")
            print("      「路由域(TIMER)与顺序域(SEQ)的 div0 实时钟是不是同一个秒」;")
            print("      若两域各漂各的, 残差会远超一个采样周期。")
            res.append(("Q-A′ 六点合并拟合残差 rms ≤600 tick（跨域同秒; 实测 %.0f）" % rr, rr <= 600))
            data["abs_hz"] = bb
            data["abs_rms"] = rr

        # ── Q2 LPF: ln(1−y) 对 tick 回归 ⇒ τ_eff ──────────────────────
        print("\n── Q2 LPF（声明 τ=2.0s → 实测 τ_eff）──")
        tau_rows = []
        for lab, wi, dv in (("div0", 0, DIV_FAST), ("div1", 2, DIV_MID), ("div2", 1, DIV_SLOW)):
            xs, zs = [], []
            for r in rec:
                y = r[1][wi]
                if 0.02 <= y <= 0.95:
                    xs.append(r[0]); zs.append(math.log(1.0 - y))
            if len(xs) < 30:
                skip.append("Q2 LPF %s —— 轨迹有效样本 %d < 30, 覆盖不足" % (lab, len(xs)))
                print("    %-5s 有效样本 %d ⇒ SKIP（不是 PASS）" % (lab, len(xs)))
                continue
            bb, aa, rms = lin(xs, zs)
            tau_eff = -S["tick_s"] / bb
            tau_pred = -dt[dv] / math.log(1.0 - dt[dv] / (2.0 + dt[dv]))   # 精确离散预言
            print("    %-5s 样本 %-5d ln(1−y) 斜率 %+.6e ⇒ τ_eff = %.4f s" % (lab, len(xs), bb, tau_eff))
            print("          声明 2.0000 s · 离散预言 %.4f s（= τ+dt/2 的精确形式）· 相对声明 %+.2f%%"
                  % (tau_pred, 100.0 * (tau_eff / 2.0 - 1.0)))
            tau_rows.append((lab, tau_eff))
            res.append(("Q2 LPF %s: 实测 τ=%.4fs 落在声明 2.0s ±3%%" % (lab, tau_eff),
                        abs(tau_eff / 2.0 - 1.0) <= 0.03))
        data["tau"] = tau_rows

        # ── Q3 PID: 输出对 tick 回归 ⇒ 每秒积分 ───────────────────────
        # ★★★ 这一条是**绝对秒的主判据**（不是 TIMER 的翻转时刻拟合）:
        #   输出 = ∫Ki·err·dt, 在 200 个连续样本上做回归 ⇒ 精度 0.01~0.08%,
        #   而翻转时刻的 6 点拟合只有 ~0.5%。同一个物理量, 观测方式决定判据的分辨率。
        print("\n── Q3 PID 积分（声明 Ki=1.0/s, err=1 ⇒ 输出就是**流逝的秒**）★ 绝对秒主判据 ──")
        pid_rows = []
        for lab, wi in (("div0", 3), ("div1", 5), ("div2", 4)):
            xs, ys = [], []
            for r in rec:
                v = r[1][wi]
                if 0.5 <= v <= 90.0:
                    xs.append(r[0]); ys.append(v)
            if len(xs) < 30:
                skip.append("Q3 PID %s —— 有效样本 %d < 30, 覆盖不足" % (lab, len(xs)))
                continue
            bb, aa, rms = lin(xs, ys)
            rate = bb * S["hz"]                     # 单位/声明秒
            print("    %-5s 样本 %-5d 斜率 %.6e 单位/tick ⇒ %.5f /s（声明 1.00000）· 相对 %+.3f%%"
                  % (lab, len(xs), bb, rate, 100.0 * (rate - 1.0)))
            pid_rows.append((lab, rate))
            res.append(("Q3 PID %s: 实测积分速率 %.5f/s 落在 1.0/s ±1%%" % (lab, rate),
                        abs(rate - 1.0) <= 0.01))
        data["pid_rate"] = pid_rows

        # ── Q5 ÷dt 量子：强制源阶跃 → 峰值 ────────────────────────────
        print("\n── Q5 ÷dt 族（源做**单调阶梯** ⇒ 峰值必须是 1/dt 的整数量子）──")
        print("    两个源（都无生产者, 走 0x24 FORCE）:")
        print("      wire[%d] 递增 0,1,2,…  → RATE（要 src 递增才得正值）" % W_FORCE)
        print("      wire[%d] 递减 250,249,… → PID-D（D 只由 d(src)/dt 定号 ⇒ 要递减）" % W_FORCE2)
        print("    两条独立通道:")
        print("      ① 锁存梯 —— HYST(value_b=0) 把瞬态变**永久状态**, **免竞态**（主判据）")
        print("      ② 轮询峰值 —— 直接读数值; 读一次串口 ~17ms 而脉冲仅 6.4ms ⇒ 只作次级")
        # ★ 第一版是"源在 0/1 间来回 + 读之前睡 15ms ⇒ 快照永远落在脉冲之后", 实测 0/200 命中。
        #   根因不是"板子慢": **观测通道的分辨率(17ms)低于被测现象的宽度(6.4ms)**;
        #   换掉观测方式（锁存）才是正解, 加大重试次数不是。
        peaks = {22: [], 23: [], 21: []}
        hits = {22: 0, 23: 0, 21: 0}
        for i in range(args.flips):
            dcl.send(CMD_FORCE, struct.pack("<HBf", W_FORCE, 1, float(i + 1)))
            dcl.send(CMD_FORCE, struct.pack("<HBf", W_FORCE2, 1, float(250 - i)))
            for _ in range(2):          # 每个阶梯连读两次 ⇒ 采样间隔 < 一个脉冲窗
                raw = rd(dcl, shm + OFF_WIRE_MAP + 4 * 21, 3)   # wire[21..23]
                if raw is None:
                    continue
                w21, w22, w23 = struct.unpack("<3f", raw[:12])
                for wi, val in ((21, w21), (22, w22), (23, w23)):
                    peaks[wi].append(val)
                    if abs(val) > 1e-6:
                        hits[wi] += 1
        # ★ 主判据: 锁存梯（免竞态）—— 读一次即可, 与采样时刻无关
        raw = rd(dcl, shm + OFF_WIRE_MAP + 4 * 40, 8)
        lat = list(struct.unpack("<8f", raw[:32])) if raw else [float("nan")] * 8
        print("\n    锁存梯（读一次就知道, 与采样时刻无关）:")
        for k, (si, thr, dst) in enumerate(LATCH):
            print("      源 wire[%-2d] > %-6.1f → 锁存 wire[%d] = %.1f" % (si, thr, dst, lat[k]))
        for si, lab, pred in ((22, "RATE  div2", exp["q_rate"]), (23, "PID-D div2", exp["q_pid"]),
                              (21, "RATE  div1", exp["q_rate1"])):
            lo, hi = LATCH_EXPECT[si]
            pat = [lat[k] >= 0.5 for k, (s2, _t, _d) in enumerate(LATCH) if s2 == si]
            want = [t <= pred for (s2, t, _d) in LATCH if s2 == si]
            print("      %-11s 图案 %s  期望 %s（阈值 %s）⇒ 峰值落在 (%g, %g]"
                  % (lab, ["1" if x else "0" for x in pat], ["1" if x else "0" for x in want],
                     [t for (s2, t, _d) in LATCH if s2 == si], lo, hi))
            res.append(("Q5 %s 锁存梯把峰值夹在 (%g, %g]（含结构值 %.2f）" % (lab, lo, hi, pred),
                        pat == want))
            data["latch_" + lab.replace("  ", "_")] = pat
        for wi, lab, pred in ((22, "RATE  div2", exp["q_rate"]), (23, "PID-D div2", exp["q_pid"]),
                              (21, "RATE  div1", exp["q_rate1"])):
            pk = max((abs(x) for x in peaks[wi]), default=0.0)
            print("    %-11s 轮询命中 %3d/%-3d 次  峰值 %9.4f   期望 %9.4f   相对 %+7.3f%%"
                  % (lab, hits[wi], len(peaks[wi]), pk, pred, 100.0 * (pk / pred - 1.0) if pred else 0.0))
            if hits[wi] < 10:
                skip.append("Q5-次级 %s 轮询峰值 —— 只命中 %d 次(<10), 观测通道没覆盖到脉冲"
                            "（主判据是锁存梯, 它已给出区间）" % (lab, hits[wi]))
            else:
                res.append(("Q5-次级 %s 轮询峰值 = %.4f（1/dt 结构值 %.4f ±2%%）" % (lab, pk, pred),
                            abs(pk / pred - 1.0) <= 0.02))
        # ★ div0 的 ÷dt 族**结构上测不到**: 脉冲宽 = 1 拍 = 100µs, 而锁存路由也是 div0
        #   —— 它和 RATE 在**同一拍**执行, 而 div0 段内 RATE 在锁存之后 ⇒ 锁存看到的
        #   永远是"上一拍"的值, 而上一拍 RATE 的输出已经是 0（脉冲早在一拍内过去了）。
        #   ⇒ 记 SKIP 并写明**为什么**, 不记 PASS。div0 的 dt 由 TIMER/LPF/PID 三条覆盖。
        print("    ★ RATE/PID-D **div0** 不判（SKIP, 不是 PASS）: 脉冲宽 = 1 拍 = 100µs,")
        print("      而锁存器也是 div0 且同拍内排在 RATE 之后 ⇒ 它读到的永远是 0。")
        print("      div0 的 dt 由 TIMER(div0 斜率)/LPF(div0 τ)/PID-I(div0 斜率) 三条覆盖。")
        skip.append("Q5 RATE/PID-D div0 —— 脉冲宽 1 拍, 同拍锁存读不到（结构性不可测）")
        data["peaks"] = {str(k): max((abs(x) for x in v), default=0.0) for k, v in peaks.items()}

    finally:
        try:
            dcl.send(CMD_STOP); time.sleep(0.2)
            dcl.send(CMD_START); time.sleep(0.3)
        except Exception:
            pass
        dcl.close()

    print("\n" + "=" * 74)
    print("=== 判据 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    for k in skip:
        print("  [SKIP] %s" % k)
    bad = [k for k, v in res if not v]
    print("\n%d 项判定, %d FAIL, %d SKIP（SKIP ≠ PASS）" % (len(res), len(bad), len(skip)))
    if args.json:
        # ★ 必须显式 encoding="utf-8": 默认走 locale(GBK) ⇒ 判据串里一个 µ/τ 就 UnicodeEncodeError
        #   —— 而且崩在**全部判据都跑完之后**, 症状是"结果都打出来了却退出码 1"。
        json.dump(dict(res=[[k, bool(v)] for k, v in res], skip=skip, data=data),
                  open(args.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("原始数据: %s" % args.json)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
