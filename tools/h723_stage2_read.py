#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_stage2_read.py — 阶段 2 单会话 A/B 测量 (ITCM vs FLASH)

为什么必须"单会话":
  两次上电 = 两个物理状态 (温度/电压/cache 预取历史)。要证明"差异来自取指
  路径而非环境", 最干净的做法是**一次上电、一条 pyocd 命令链**, 在同一个
  固件里切换运行期选择器 —— 两组数据出自同一个 CPU 状态。

★ 前提已静态验证: 两份扫描实现**逐指令相同**
  (engine.c 一个宏实例化两次; 已用 build/dcl_h723.bin 逐字节比对 = 2136B 全同)
  所以任何周期数差异只能归因于 ① 取指路径 (ITCM 零等待 vs flash+L1 cache)
  ② 长跳转 veneer (ITCM→FLASH 超过 BL 的 ±16MB 范围, 需要跳板)。

用法:
  python tools/h723_stage2_read.py                # 完整 8 组
  python tools/h723_stage2_read.py --quick        # 4 组核心对比
  python tools/h723_stage2_read.py --dur 0.5      # 每组采样时长
"""
import os, re, sys, argparse, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
      "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update"
      ".win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")

CPU_HZ = 400_000_000
TICK_CYC = 40000            # 100μs @400MHz
S3_BASELINE_DIRECT = 234    # esp32-core0 实测: CONST→DIRECT 路由 234 cyc @240MHz

HDR_SYMS = [
    "g_boot_status", "g_stage", "g_tick_count", "g_clock_hclk",
    "g_isr_itcm", "g_shm_ok", "g_shm_addr",
    "g_shm_start_addr", "g_shm_end_addr",
    "g_scan_itcm_addr", "g_scan_flash_addr",
    "g_reinit_done", "g_dwt_overhead", "g_cal_n1000",
    "g_icache_on", "g_ccr_before", "g_ccr_after",
]
# (符号, word 数) —— 顺序 = 读回顺序
STAT_SYMS = [
    ("g_eng_cyc_min", 1), ("g_eng_cyc_max", 1), ("g_eng_cyc_last", 1),
    ("g_eng_cyc_sum", 2), ("g_eng_n", 1), ("g_eng_div0", 1),
    ("g_isr_cyc_min", 1), ("g_isr_cyc_max", 1), ("g_isr_cyc_last", 1),
    ("g_isr_cyc_sum", 2), ("g_isr_n", 1),
    ("g_eng_sel_used", 1), ("g_eng_n_used", 1),
    ("g_per_cyc_min", 1), ("g_per_cyc_max", 1), ("g_per_cyc_last", 1),
    ("g_table_ck", 1), ("g_active_routes", 1), ("g_guard_ok", 1),
    ("g_bucket_ck", 1), ("g_bucket_zero_slots", 1),
    ("g_eng_routes_last", 1), ("g_eng_routes_total", 2), ("g_eng_ticks", 1),
    ("g_eng_cyc_first", 1), ("g_isr_cyc_first", 1),      # ★ H5: 首样本留痕
]
TAIL_SYMS = ["g_eng_ck"]

# ═══════════════════════════════════════════════════════════════════════
# 表内容独立预测 (审计修正, 对照 S3 第二十七轮 OA23 "判据必须可失败")
#   固件里 g_table_ck 是 FNV-1a 校验和; 这里**按 fill_tables 的逻辑独立重算**,
#   拿预测值与实测值比对 —— 这才是"表被正确装载"的正向证据。
#   旧版哨兵 (route[0].op) 在 profile 0 与 profile 1 上都是 0 → 判据恒真。
# ═══════════════════════════════════════════════════════════════════════
OP_DIRECT, OP_CMP, OP_HYST, OP_CLAMP, OP_LPF = 0x00, 0x01, 0x02, 0x03, 0x04
OP_PID, OP_RATE, OP_DEADBAND, OP_MUX, OP_EDGE = 0x05, 0x06, 0x07, 0x08, 0x09
OP_LUT, OP_CNT, OP_TIMER, OP_ARITH, OP_SCALE = 0x0A, 0x0B, 0x0C, 0x0D, 0x0E
OP_AND, OP_OR, OP_NOT, OP_SR = 0x0F, 0x10, 0x11, 0x12
MIXED_OPS = [OP_DIRECT, OP_CMP, OP_CLAMP, OP_SCALE, OP_AND, OP_OR, OP_NOT, OP_MUX,
             OP_LUT, OP_LPF, OP_PID, OP_HYST, OP_RATE, OP_DEADBAND, OP_EDGE, OP_CNT,
             OP_TIMER, OP_ARITH, OP_SR]
MAX_ROUTES = 128
MAX_STATES = 128                 # ★ 与 engine.h 一致 (state_offset 用 1..127, 0 = 无槽)
STATEFUL_OPS = {OP_LPF, OP_PID, OP_HYST, OP_RATE, OP_DEADBAND, OP_EDGE,
                OP_CNT, OP_TIMER, OP_SR}


# ═══════════════════════════════════════════════════════════════════════
# 阶段 3: 桶表 + 每拍执行条数的独立预测 (复刻 engine_build_buckets / engine_tick)
#   与 g_bucket_ck / g_eng_routes_last 逐组比对 —— 让"分档调度真的只跑本拍桶"
#   成为**可失败**的判据 (若退回全表扫, rlast 会等于 128, 不在可行值集合里)。
# ═══════════════════════════════════════════════════════════════════════
BUCKET_DIV1_PHASES = 10
BUCKET_DIV2_PHASES = 64          # ★ 与固件一致 (6 位 phase 字段)
ROUTE_BUCKET_U16 = 220


def _route_divphase(profile, i):
    """复刻 engine_fill_tables 的档位/相位分配"""
    dv, ph = 0, 0
    if profile in (3, 4):
        g, m = i // 3, i % 3
        if m == 1:
            dv, ph = 1, g % BUCKET_DIV1_PHASES
        elif m == 2:
            dv, ph = 2, g % BUCKET_DIV2_PHASES
    return dv, ph


def predict_buckets(profile):
    """返回 (FNV 校验和, 不可达槽非零个数, off1, cnt1, off2, cnt2)"""
    n0 = 0
    cnt1 = [0] * BUCKET_DIV1_PHASES
    cnt2 = [0] * BUCKET_DIV2_PHASES
    for i in range(MAX_ROUTES):
        dv, ph = _route_divphase(profile, i)
        if dv == 0:
            n0 += 1
        elif dv == 1:
            cnt1[ph] += 1
        else:
            cnt2[ph] += 1
    off1, acc = [0] * BUCKET_DIV1_PHASES, n0
    for p in range(BUCKET_DIV1_PHASES):
        off1[p] = acc
        acc += cnt1[p]
    off2 = [0] * BUCKET_DIV2_PHASES
    for p in range(BUCKET_DIV2_PHASES):
        off2[p] = acc
        acc += cnt2[p]
    # 桶表 = 220 u16: [off1[10]][cnt1[10]][off2[100]][cnt2[100]]
    #   ★ 固件只填 off2/cnt2 的 0..63, 64..99 恒 0 (H9)
    table = off1 + cnt1 + off2 + [0] * (100 - BUCKET_DIV2_PHASES) \
            + cnt2 + [0] * (100 - BUCKET_DIV2_PHASES)
    assert len(table) == ROUTE_BUCKET_U16, len(table)
    h = 0x811C9DC5
    for v in table:
        h = ((h ^ (v & 0xFFFF)) * 16777619) & 0xFFFFFFFF
    return h, 0, off1, cnt1, off2, cnt2


def predict_routes_at(profile, tick):
    """本拍应执行的路由条数 = div0 全部 + div1 桶 + div2 桶"""
    _h, _d, off1, cnt1, off2, cnt2 = predict_buckets(profile)
    return off1[0] + cnt1[tick % BUCKET_DIV1_PHASES] + cnt2[tick % BUCKET_DIV2_PHASES]


def _route_fields(profile, i):
    """复刻 engine_fill_tables 单条路由的 op/src_type/state_offset/flags"""
    if profile == 1 or profile == 4:
        op = MIXED_OPS[i % 19]
    elif profile == 2:
        op = OP_PID
    elif profile >= 100 and 0 <= (profile - 100) <= OP_SR:
        op = profile - 100            # ★ 单一原语模式 (成本表实测用)
    else:
        op = OP_DIRECT
    src_type = 0 if i % 3 == 0 else (1 if i % 3 == 1 else 2)
    # ★ A3/H11 后必须与 engine_fill_tables 逐条对齐:
    #   · state_offset 避开 0 (0 是"无槽"哨兵) → (i % (MAX_STATES-1)) + 1
    #   · 双输入原语要置 ROUTE_FLAG_WIRE2 (0x02) —— 否则 ISR 的 wire2_valid()
    #     在 wire2_idx==0 时会判"无第二输入", 与填表意图打架
    state_offset = ((i % (MAX_STATES - 1)) + 1) if op in STATEFUL_OPS else 0
    flags = 1 | (0x02 if op in WIRE2_OPS else 0)      # 1 = ROUTE_FLAG_ACTIVE
    return op, src_type, state_offset, flags


DST_WIRE = 2
STATE_OFF_MAX = 128
WIRE2_OPS = {OP_AND, OP_OR, OP_ARITH, OP_SR, OP_CNT}


def _route_bytes(profile, i):
    """复刻 engine_fill_tables 单条路由的 **16 字节** 打包 (RouteEntry_t, packed LE)"""
    op, src_type, state_offset, flags = _route_fields(profile, i)
    dv, ph = _route_divphase(profile, i)
    b = bytearray(16)
    b[0] = src_type
    b[1] = i % 64                      # src_index
    b[2] = DST_WIRE                    # dst_type
    b[3] = i % 128                     # dst_channel
    b[4] = op
    b[5] = flags
    b[6:8] = (i % 128).to_bytes(2, "little")            # param_idx
    b[8:10] = state_offset.to_bytes(2, "little")
    b[10:12] = (0).to_bytes(2, "little")                # actuator_idx
    b[12:14] = ((i + 7) % 128 if op in WIRE2_OPS else 0).to_bytes(2, "little")
    b[14] = (dv | (ph << 2)) & 0xFF                     # period  ← offset 14, 不是 15!
    b[15] = 0                                           # reserved (S3 的尾部填充)
    return bytes(b)


def _bucket_order(profile):
    """复刻 engine_build_buckets: [div0 (源序)][div1 phase0..9][div2 phase0..63]
    —— 桶内保持源序 (稳定排序), 所以对整个表再跑一次是恒等变换 (幂等)。"""
    items = []
    for i in range(MAX_ROUTES):
        dv, ph = _route_divphase(profile, i)
        items.append((dv, ph, _route_bytes(profile, i)))
    out = [it for it in items if it[0] == 0]
    for p in range(BUCKET_DIV1_PHASES):
        out += [it for it in items if it[0] == 1 and it[1] == p]
    for p in range(BUCKET_DIV2_PHASES):
        out += [it for it in items if it[0] == 2 and it[1] == p]
    return out


def predict_table(profile):
    """返回 (FNV-1a 校验和, ACTIVE 条数) —— 复刻 engine_fill_tables
    ★ 逐字节哈希整个 16B 条目, 覆盖全部字段 (含 period) 与归组后的表序"""
    h, active = 0x811C9DC5, 0
    for (_dv, _ph, raw) in _bucket_order(profile):
        for byte in raw:
            h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
        active += 1
    return h, active
ITCM_VERIFY_WORDS = 64      # 每次比对 256 字节

# 配置矩阵: (代号, 标签, gate, sel, profile, n, icache, scan_mode)
#   scan_mode: 0 = 全表扫 (阶段 2 基线) / 1 = 分档调度 (阶段 3)
#   ★ icache 只能从 0→1 单向切换, 所以所有 icache=0 的组必须排在前面
#   ★ 代号必须显式带上 —— 之前用列表下标映射代号, --quick 子集下全部错位
CONFIGS_FULL = [
    ("A", "骨架 (gate=0 不扫描)          ", 0, 0, 0, 128, 0, 0),
    ("B1", "FLASH 全表128·全DIRECT ·IC关  ", 1, 0, 0, 128, 0, 0),
    ("B2", "ITCM  全表128·全DIRECT ·IC关  ", 1, 1, 0, 128, 0, 0),
    ("C1", "FLASH 全表128·19原语轮转·IC关 ", 1, 0, 1, 128, 0, 0),
    ("C2", "ITCM  全表128·19原语轮转·IC关 ", 1, 1, 1, 128, 0, 0),
    ("D1", "FLASH 半表 64·全DIRECT ·IC关  ", 1, 0, 0, 64, 0, 0),
    ("D2", "ITCM  半表 64·全DIRECT ·IC关  ", 1, 1, 0, 64, 0, 0),
    ("E", "ITCM  全表128·全PID    ·IC关  ", 1, 1, 2, 128, 0, 0),
    ("F1", "FLASH 全表128·全DIRECT ·IC开★ ", 1, 0, 0, 128, 1, 0),
    ("F2", "ITCM  全表128·全DIRECT ·IC开★ ", 1, 1, 0, 128, 1, 0),
    ("F3", "FLASH 半表 64·全DIRECT ·IC开★ ", 1, 0, 0, 64, 1, 0),
]
_QM = {"A", "B1", "B2", "F1", "F2"}
CONFIGS_QUICK = [c for c in CONFIGS_FULL if c[0] in _QM]
# 落位敏感扫描用: 只要 骨架 / FLASH / ITCM 三条 + 混合组, 单次跑 ~3s
_SW = {"A", "B1", "B2", "C1"}
# 阶段 3: 分档调度 —— 同一份三档程序, 分档 vs 全表 直接对照
CONFIGS_FULL += [
    ("G1", "ITCM 分档·三档DIRECT(prof3)", 1, 1, 3, 128, 0, 1),
    ("G2", "ITCM 全表·三档DIRECT(prof3)", 1, 1, 3, 128, 0, 0),
    ("G3", "ITCM 分档·三档混合(prof4)  ", 1, 1, 4, 128, 0, 1),
    ("G4", "ITCM 全表·三档混合(prof4)  ", 1, 1, 4, 128, 0, 0),
]
CONFIGS_SWEEP = [c for c in CONFIGS_FULL if c[0] in _SW]


def s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def symbols(elf):
    out = subprocess.run([NM, "-S", elf], capture_output=True, text=True, timeout=60)
    addr, size = {}, {}
    for line in out.stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            addr[p[2]] = int(p[0], 16)
        elif len(p) == 4:
            addr[p[3]] = int(p[0], 16)
            size[p[3]] = int(p[1], 16)
    return addr, size


def rd(addr):
    return "read32 0x%08X" % addr


def run(sym, configs, dur):
    cmd = ["reset", "sleep 300"]

    # ---- [0] 落位自检 + ITCM 复制验证 ----
    for n in HDR_SYMS:
        cmd.append(rd(sym[n]))
    N = ITCM_VERIFY_WORDS
    if ("_sitcm" in sym) and ("_siitcm" in sym):
        cmd.append("read32 0x%08X %d" % (sym["_sitcm"], N * 4))
        cmd.append("read32 0x%08X %d" % (sym["_siitcm"], N * 4))
    cmd.append("read32 0x%08X %d" % (sym["engine_scan_itcm"], N * 4))
    cmd.append("read32 0x%08X %d" % (sym["engine_scan_flash"], N * 4))

    # ---- 逐组 ----
    # ★ 命令顺序很关键 (第一版踩过):
    #   先 gate=0 → 换表 → 设 sel/n → **先开/关好 gate** → 再清统计 → 再采样。
    #   若先清统计再写 gate, 两条 SWD 写之间会夹进若干拍 (100μs/拍), 这些
    #   "gate 还是旧值"的拍会把 isr_min 污染成骨架值 (实测差 ~26000 cyc)。
    cur_profile = 0
    icache_on = False
    for (code, label, gate, sel, prof, n, ic, mode) in configs:
        if ic != (1 if icache_on else 0):
            if ic == 1:
                cmd.append("write32 0x%08X 1" % sym["g_icache_req"])
                cmd.append("sleep 200")
                icache_on = True
        cmd.append("write32 0x%08X 0" % sym["g_engine_gate"])          # 先停扫描
        if prof != cur_profile:
            cmd.append("write32 0x%08X %d" % (sym["g_table_profile"], prof))
            cmd.append("write32 0x%08X 1" % sym["g_reinit"])          # 请求重填表
            cmd.append("sleep 300")
            cur_profile = prof
        cmd.append("write32 0x%08X %d" % (sym["g_engine_sel"], sel))
        cmd.append("write32 0x%08X %d" % (sym["g_scan_mode"], mode))
        cmd.append("write32 0x%08X %d" % (sym["g_n_routes"], n))
        cmd.append("write32 0x%08X %d" % (sym["g_engine_gate"], gate))  # ★ 先定 gate
        cmd.append("sleep 30")                                         # 让两组状态分离
        cmd.append("write32 0x%08X 1" % sym["g_stat_reset"])          # ★ 再清统计
        cmd.append("sleep %d" % int(dur * 1000))
        for nm, w in STAT_SYMS:
            # ★ 多字变量 (u64 sum) 必须逐字读 —— 少读一个字会让整条解析链错位
            for k in range(w):
                cmd.append(rd(sym[nm] + 4 * k))
        cmd.append(rd(sym["g_stage"]))                                # 守卫
    for n in TAIL_SYMS:
        cmd.append(rd(sym[n]))

    args = ["pyocd", "cmd", "-t", "stm32h723xx",
            "-O", "connect_mode=under-reset"]
    for c in cmd:
        args += ["-c", c]
    r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    log = os.path.join(ROOT, "build", "stage2_raw.txt")
    try:
        open(log, "w", encoding="utf-8").write(r.stdout + "\n===== STDERR =====\n" + r.stderr)
    except Exception:
        pass
    vals = parse_reads(r.stdout)
    return vals, r.stdout + r.stderr, cmd


def parse_reads(stdout):
    """pyocd read32 行格式: '20000008:  0000002b          |...+|'
    ★ 必须剥掉尾部的 ASCII 转储列 —— 带 $ 行尾锚点的旧正则会一行都匹配不到
      (阶段 1 的脚本用 re.match 无锚点所以没事; 这里加过锚点, 踩了一次)。"""
    vals = []
    for line in stdout.splitlines():
        m = re.match(r"^\s*([0-9a-f]{8}):(.*)$", line)
        if not m:
            continue
        body = m.group(2)
        if "|" in body:                 # 去掉 ASCII 转储列
            body = body.split("|")[0]
        vals += [int(x, 16) for x in re.findall(r"\b[0-9a-f]{8}\b", body)]
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=0.6, help="每组采样时长 (秒)")
    ap.add_argument("--elf", default=os.path.join(ROOT, "build", "dcl_h723"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--sweep", action="store_true",
                    help="只跑 骨架/FLASH/ITCM/混合 4 组 (落位扫描用, ~3s)")
    ap.add_argument("--json", default="", help="把关键数字写成 JSON (供批量脚本汇总)")
    a = ap.parse_args()

    sym, size = symbols(a.elf)
    if not sym:
        print("!! nm 解析不到符号 (检查 ELF 路径)"); return 2

    if a.sweep:
        configs = CONFIGS_SWEEP
    else:
        configs = CONFIGS_QUICK if a.quick else CONFIGS_FULL
    n_stats = sum(w for _, w in STAT_SYMS)
    n_verify = ITCM_VERIFY_WORDS * (4 if ("_sitcm" in sym) else 2)
    expect = len(HDR_SYMS) + n_verify + len(configs) * (n_stats + 1) + len(TAIL_SYMS)

    print("=" * 80)
    print("阶段 2 单会话 A/B 测量  (dur=%.2fs/组, %d 组)" % (a.dur, len(configs)))
    print("=" * 80)

    ai, af = sym.get("engine_scan_itcm", 0), sym.get("engine_scan_flash", 0)
    si, sf = size.get("engine_scan_itcm", 0), size.get("engine_scan_flash", 0)
    print("[静态] engine_scan_itcm  @ 0x%08X  size=%d" % (ai, si))
    print("[静态] engine_scan_flash @ 0x%08X  size=%d" % (af, sf))
    if si != sf:
        print("       !! 两份尺寸不同 → 实例化被优化掉, A/B 无效")
    h = sym.get("TIM2_IRQHandler", 0)
    print("[静态] TIM2_IRQHandler   @ 0x%08X  (%s)"
          % (h, "ITCM" if h < 0x10000 else "FLASH"))

    vals, raw, _cmd = run(sym, configs, a.dur)
    if len(vals) != expect:
        print("!! 读回 %d 个, 期望 %d 个 —— 解析可能不完整" % (len(vals), expect))
        print(raw[-1500:])
        if len(vals) < expect:
            return 2
    it = iter(vals)
    hdr = {n: next(it) for n in HDR_SYMS}
    N = ITCM_VERIFY_WORDS
    itcm_w = lma_w = None
    if ("_sitcm" in sym) and ("_siitcm" in sym):
        itcm_w = [next(it) for _ in range(N)]
        lma_w = [next(it) for _ in range(N)]
    scan_itcm_w = [next(it) for _ in range(N)]
    scan_flash_w = [next(it) for _ in range(N)]

    print("\n" + "─" * 80)
    print("① 落位自检")
    print("─" * 80)
    print("  boot_status=%d (0=OK)  stage=%d (9=主循环)  tick=%d  HCLK=%d Hz"
          % (s32(hdr['g_boot_status']), hdr['g_stage'], hdr['g_tick_count'], hdr['g_clock_hclk']))
    print("  ISR_ITCM(编译期)=%d   g_shm @ 0x%08X   shm_layout_ok=%d (需 1)"
          % (hdr['g_isr_itcm'], hdr['g_shm_addr'], hdr['g_shm_ok']))
    print("  ★外部权威比对 (不采信固件自报):")
    print("      g_shm        = 0x%08X" % hdr['g_shm_addr'])
    print("      _shm_start   = 0x%08X   (ELF 符号 = 0x%08X)  %s"
          % (hdr['g_shm_start_addr'], sym.get('_shm_start', 0),
             "✓" if hdr['g_shm_start_addr'] == sym.get('_shm_start') == hdr['g_shm_addr']
             else "✗"))
    print("      _shm_end     = 0x%08X   (ELF 符号 = 0x%08X)  %s"
          % (hdr['g_shm_end_addr'], sym.get('_shm_end', 0),
             "✓" if hdr['g_shm_end_addr'] == sym.get('_shm_end') else "✗"))
    print("      段长 = 0x%X (期望 0x8000)  DTCM 域 0x20000000-0x20020000  %s"
          % (hdr['g_shm_end_addr'] - hdr['g_shm_start_addr'],
             "✓" if (0x20000000 <= hdr['g_shm_addr']
                     < hdr['g_shm_addr'] + 0x8000 <= 0x20020000) else "✗"))
    print("  engine_scan_itcm  @ 0x%08X (需 <0x10000) / flash @ 0x%08X (需 ≥0x08000000)"
          % (hdr['g_scan_itcm_addr'], hdr['g_scan_flash_addr']))
    if itcm_w is not None:
        same = sum(1 for x, y in zip(itcm_w, lma_w) if x == y)
        print("  ★ITCM 复制验证: .itcm_text 前 %d 字 vs flash 装载映像 相同 %d/%d  %s"
              % (N, same, N, "✓ 启动拷贝已生效" if same == N else "✗ 拷贝没跑"))
    s_itcm = sum(1 for x, y in zip(scan_itcm_w, scan_flash_w) if x == y)
    print("  ★A/B 前提验证: engine_scan_itcm vs engine_scan_flash 前 %d 字 相同 %d/%d  %s"
          % (N, s_itcm, N,
             "✓ 两份实现逐字相同 (差异只可能来自取指路径)"
             if s_itcm == N else "✗ 两份实现不同 → A/B 不成立"))
    print("  reinit_done=%d" % hdr['g_reinit_done'])
    print("  ★表内容独立预测 (Python 复刻 fill_tables, 用于逐组比对):")
    for p in (0, 1, 2):
        ck, ac = predict_table(p)
        print("      profile %d → 预测校验和 0x%08X, ACTIVE %d 条" % (p, ck, ac))
    print("  DWT 读对开销=%d cyc   nop×1000=%d cyc (%.2f cyc/迭代)"
          % (hdr['g_dwt_overhead'], hdr['g_cal_n1000'], hdr['g_cal_n1000'] / 1000.0))
    print("  L1 I-cache: on=%d  CCR: 0x%08X → 0x%08X   (I-cache 位 = %s)"
          % (hdr['g_icache_on'], hdr['g_ccr_before'], hdr['g_ccr_after'],
             "置位" if (hdr['g_ccr_after'] >> 17) & 1 else "未置位"))

    rows = []
    for (code, label, gate, sel, prof, n, ic, mode) in configs:
        st = {}
        for nm, w in STAT_SYMS:
            v = 0
            for k in range(w):
                v |= next(it) << (32 * k)
            st[nm] = v
        next(it)  # guard (g_stage)
        rows.append(dict(code=code, label=label.strip(),
                         gate=gate, sel=sel, prof=prof, n=n, ic=ic, mode=mode,
                         emin=0 if st['g_eng_cyc_min'] == 0xFFFFFFFF else st['g_eng_cyc_min'],
                         emax=st['g_eng_cyc_max'], elast=st['g_eng_cyc_last'],
                         esum=st['g_eng_cyc_sum'], en=st['g_eng_n'], ediv0=st['g_eng_div0'],
                         imin=0 if st['g_isr_cyc_min'] == 0xFFFFFFFF else st['g_isr_cyc_min'],
                         imax=st['g_isr_cyc_max'], ilast=st['g_isr_cyc_last'],
                         isum=st['g_isr_cyc_sum'], inum=st['g_isr_n'],
                         sel_used=st['g_eng_sel_used'], n_used=st['g_eng_n_used'],
                         efirst=st['g_eng_cyc_first'], ifirst=st['g_isr_cyc_first'],
                         pmin=0 if st['g_per_cyc_min'] == 0xFFFFFFFF else st['g_per_cyc_min'],
                         pmax=st['g_per_cyc_max'], plast=st['g_per_cyc_last'],
                         tck=st['g_table_ck'], active=st['g_active_routes'],
                         guard=st['g_guard_ok'],
                         bck=st['g_bucket_ck'], bdead=st['g_bucket_zero_slots'],
                         rlast=st['g_eng_routes_last'], rtot=st['g_eng_routes_total'],
                         rtick=st['g_eng_ticks']))
    tail = {n: next(it) for n in TAIL_SYMS}
    d = {r['code']: r for r in rows}

    def per(r):
        return (r['emin'] / float(r['n'])) if r['n'] else 0.0

    print("\n" + "=" * 80)
    print("② 每拍成本 (CPU 周期 @400MHz, 拍预算 40000 cyc = 100μs)")
    print("=" * 80)
    # ★ H5 口径修正: 本项目的权威数字是 **max** (WCET), 不是 min。
    #   min 从 0xFFFFFFFF 起算, 所以"上电后第一拍"必然成为 min —— 若 min == first,
    #   这个 min 只是启动期非稳态样本, 不能当"稳态最小成本"用 (旧报告拿它算净成本,
    #   报的是最乐观值, 方向与"确定性/WCET"正好相反)。
    print("  %-4s %-28s %8s %8s %9s %10s %10s" %
          ("", "配置", "eng_min", "eng_max", "eng_mean", "ISR最坏", "最坏占拍"))
    for r in rows:
        mean = (r['esum'] / float(r['en'])) if r['en'] else 0.0
        ov = " ★超载" if r['imax'] > TICK_CYC else ""
        minsus = "?" if (r['emin'] and r['efirst'] == r['emin']) else " "
        print("  %-4s %-28s %8d%s %8d %9.1f %10d %9.2f%%%s"
              % (r['code'], r['label'], r['emin'], minsus, r['emax'], mean, r['imax'],
                 100.0 * r['imax'] / TICK_CYC, ov))
    if any(r['emin'] and r['efirst'] == r['emin'] for r in rows):
        print("  ★ eng_min 后带 ? = 该 min **等于首样本** (上电/清统计后的第一拍), "
              "\n     属非稳态样本, 不能当稳态最小成本; 权威口径请看 eng_max 与 ISR最坏。")
    print("  ★超载 = ISR 最坏耗时 > 40000 cyc → 这一组的**拍周期统计不再是自由运行定时器**,"
          "\n     而是被 ISR 拉长 (见 ⑤); 引擎成本仍有效, 但已越出确定性设计的适用边界。")

    print("\n" + "=" * 80)
    print("③ 单条路由成本 — 两点法 (128条 − 64条, 除掉调用/循环常数)")
    print("=" * 80)

    def slope(c128, c64):
        if c128 in d and c64 in d:
            a, b = d[c128], d[c64]
            return (a['emin'] - b['emin']) / float(a['n'] - b['n'])
        return None

    print("  %-8s %10s %10s %12s" % ("取指", "128条(cyc)", "64条(cyc)", "cyc/条"))
    for tag, c1, c2 in (("FLASH·IC关", "B1", "D1"), ("ITCM ·IC关", "B2", "D2"),
                        ("FLASH·IC开", "F1", "F3")):
        s = slope(c1, c2)
        if s is not None:
            print("  %-8s %10d %10d %12.2f"
                  % (tag, d[c1]['emin'], d[c2]['emin'], s))

    print("\n  ★ 取指路径的代价 (128 条全表 · 全DIRECT):")
    if "B1" in d and "B2" in d:
        f, i = d["B1"], d["B2"]
        print("      FLASH %d cyc (%d%% 拍)  vs  ITCM %d cyc (%d%% 拍)"
              % (f['emin'], round(100.0 * f['emin'] / TICK_CYC),
                 i['emin'], round(100.0 * i['emin'] / TICK_CYC)))
        print("      → 差 %d cyc = %.1f μs,  ITCM 快 %.1f 倍"
              % (f['emin'] - i['emin'], (f['emin'] - i['emin']) / CPU_HZ * 1e6,
                 f['emin'] / float(i['emin'])))
        print("      → 单条: FLASH %.1f cyc  vs  ITCM %.1f cyc"
              % (per(f), per(i)))

    if "F1" in d and "F2" in d:
        f, i = d["F1"], d["F2"]
        print("\n  ★ 打开 L1 I-cache 之后:")
        print("      FLASH %d cyc (%d%% 拍)  vs  ITCM %d cyc (%d%% 拍)"
              % (f['emin'], round(100.0 * f['emin'] / TICK_CYC),
                 i['emin'], round(100.0 * i['emin'] / TICK_CYC)))
        if "B1" in d:
            print("      FLASH: IC关 %d → IC开 %d cyc  (加速 %.1f 倍)"
                  % (d["B1"]['emin'], f['emin'], d["B1"]['emin'] / float(f['emin'])))
        if "B2" in d:
            print("      ITCM : IC关 %d → IC开 %d cyc  (变化 %.1f%% — ITCM 不经 cache, 理应不变)"
                  % (d["B2"]['emin'], i['emin'],
                     100.0 * (i['emin'] - d["B2"]['emin']) / d["B2"]['emin']))
        if "B1" in d:
            print("      ★ 且 FLASH 版开着 cache 才 %.1f cyc/条, 仍高于 ITCM 的 %.1f"
                  % (per(f), per(i)))
        b1, f1 = d.get("B1"), d.get("F1")
        if b1 and f1:
            print("      ★ 抖动看极差: FLASH·IC关 %d cyc, FLASH·IC开 %d cyc, ITCM·IC关 %d cyc"
                  % (b1['emax'] - b1['emin'], f1['emax'] - f1['emin'],
                     d["B2"]['emax'] - d["B2"]['emin']))

    print("\n  ★ 混合程序 (19 原语轮转) / 最重档 (全 PID):")
    for c, name in (("C2", "混合·ITCM"), ("C1", "混合·FLASH"), ("E", "全PID·ITCM")):
        if c in d:
            print("      %-12s eng_min=%6d cyc → %6.2f cyc/条   (最坏占拍 %.1f%%)"
                  % (name, d[c]['emin'], per(d[c]), 100.0 * d[c]['imax'] / TICK_CYC))

    print("\n  对照 esp32-core0 (@240MHz): CONST→DIRECT 单条 = %d cyc = %.0f ns"
          % (S3_BASELINE_DIRECT, S3_BASELINE_DIRECT / 240e6 * 1e9))
    if "B2" in d:
        print("      H723 ITCM 单条 ≈ %.1f cyc @400MHz = %.0f ns  (含调用+探针+循环常数)"
              % (per(d["B2"]), per(d["B2"]) / CPU_HZ * 1e9))

    print("\n" + "=" * 80)
    print("⑤ 拍周期 (硬件定时器自由运行 —— **前提是 ISR 在拍内跑完**)")
    print("=" * 80)
    print("  %-4s %-30s %8s %8s %8s %10s  %s" %
          ("", "配置", "周期min", "周期max", "极差", "极差(ns)", "判定"))
    n_over = 0
    for r in rows:
        if r['pmin']:
            # ★ 超载的判据是"ISR 装不下" (isr_max > 拍长), **不是** 周期 max 略大于 40000。
            #   后者会把"某个 tick 的入口延迟了几十 ns"误报成超载 (实测踩过: 40012 被误判)。
            #   真正的超载会让周期涨到 ISR 时长量级 (数千 cyc), 判据必须能区分这两种。
            over = r['imax'] > TICK_CYC
            if over:
                n_over += 1
            print("  %-4s %-30s %8d %8d %8d %10.1f  %s"
                  % (r['code'], r['label'], r['pmin'], r['pmax'],
                     r['pmax'] - r['pmin'], (r['pmax'] - r['pmin']) / CPU_HZ * 1e9,
                     "★超载: 拍被 ISR 拉长" if over
                     else "拍内 (isr_max %d < 40000)" % r['imax']))
    print()
    if n_over == 0:
        print("  → ISR 全部装得进拍 ⇒ 拍周期恒 40000 (在拍内跑完的前提下)")
    else:
        print("  → %d 组 ISR 装不进拍 ⇒ **不能再说「硬拍与负载完全解耦」**:"
              "\n     解耦成立的前提是 ISR 在拍内跑完; 一旦超载, 拍周期改由 ISR 时长决定,"
              "\n     而且不再确定 (实测极差 %d cyc)。这反转了本报告的旧口径。"
              % (n_over, max((r['pmax'] - r['pmin']) for r in rows
                             if r['pmin'] and r['imax'] > TICK_CYC)))
    print("  注: 骨架/ITCM 组一律 40000 整 —— ITCM 的成本与负载无关, 不参与超载。")

    print("\n" + "=" * 80)
    print("⑥ 阶段 3: 分档调度 vs 全表扫 (同一份三档程序, 只切换扫描方式)")
    print("=" * 80)
    for ca, cb, nm in (("G1", "G2", "三档·全DIRECT"), ("G3", "G4", "三档·19原语混合")):
        if ca in d and cb in d:
            ra, rb = d[ca], d[cb]
            _, _, o1, c1, o2, c2 = predict_buckets(ra['prof'])
            print("  %-16s  分档 eng_min=%6d (本拍%2d 条, 最坏占拍 %5.2f%%)"
                  % (nm, ra['emin'], ra['rlast'], 100.0 * ra['imax'] / TICK_CYC))
            print("  %-16s  全表 eng_min=%6d (本拍%2d 条, 最坏占拍 %5.2f%%)"
                  % ("", rb['emin'], rb['rlast'], 100.0 * rb['imax'] / TICK_CYC))
            if ra['emin']:
                print("  %-16s  条数比 %.2f×   成本比 %.2f×   ← 两者应一致"
                      % ("", rb['rlast'] / float(max(ra['rlast'], 1)),
                         rb['emin'] / float(ra['emin'])))
            print()
    if "G1" in d:
        _, _, o1, c1, o2, c2 = predict_buckets(3)
        print("  桶表 (profile 3): div0 每拍 %d 条 | div1 桶 %s | div2 共 %d 条分布在 %d 个相位"
              % (o1[0], c1, sum(c2), BUCKET_DIV2_PHASES))

    print("\n" + "=" * 80)
    print("⑦ 守卫 (必须全 ✓, 否则本组数据不可信)")
    print("=" * 80)
    for r in rows:
        exp_ck, exp_ac = predict_table(r['prof'])
        exp_bck, exp_dead, _o1, _c1, _o2, _c2 = predict_buckets(r['prof'])
        feas = set(predict_routes_at(r['prof'], t) for t in range(320))
        ck_ok = (r['tck'] == exp_ck)          # ★表内容与独立预测一致
        ac_ok = (r['active'] == exp_ac)       # ★ACTIVE 条数与预测一致
        gd_ok = (r['guard'] == 1)             # ★栈没踩到表
        bk_ok = (r['bck'] == exp_bck and r['bdead'] == exp_dead)   # ★桶表与独立预测一致
        # ★行为判据 (可失败):
        #   分档 → 本拍条数必须在桶表允许的**可行值集合**里, 且均值也落在区间内。
        #          若退回全表扫, rlast 会是 128, 不在 {47,48,49} 里 → 立刻暴露。
        #   全表 → 本拍条数必须恰好等于 n。
        rl_ok = True
        if r['gate']:
            if r['mode']:
                rl_ok = (r['rlast'] in feas)
                if r['rtick']:
                    avg = r['rtot'] / float(r['rtick'])
                    rl_ok = rl_ok and (min(feas) - 0.01 <= avg <= max(feas) + 0.01)
            else:
                rl_ok = (r['rlast'] == r['n'])
        if r['gate']:
            ok = (r['ediv0'] == 0 and r['en'] > 0 and r['sel_used'] == r['sel']
                  and ck_ok and ac_ok and gd_ok and bk_ok and rl_ok)
            why = "表ck=%s 桶ck=%s/%s 槽64-99=%d 本拍条数=%d%s%s%s%s" % (
                "✓" if ck_ok else "✗",
                "✓" if r['bck'] == exp_bck else "✗0x%08X≠0x%08X" % (r['bck'], exp_bck),
                "分档" if r['mode'] else "全表",
                r['bdead'], r['rlast'],
                " 可行值%s" % sorted(feas) if r['mode'] else " (应=%d)" % r['n'],
                "" if rl_ok else " ✗条数不在可行集!",
                "" if ac_ok else " ✗ACTIVE=%d≠%d" % (r['active'], exp_ac),
                "" if gd_ok else " ✗哨兵被踩")
        else:
            ok = (r['en'] == 0)
            why = "eng_n=%d (骨架组必须 0)  表ck=%s 哨兵=%s" % (
                r['en'], "✓" if ck_ok else "✗", "✓" if gd_ok else "✗")
        print("  %-4s %-30s %-56s %s" % (r['code'], r['label'], why, "✓" if ok else "✗"))

    print("\n  ★行为证据 (对照旧版「判据恒真」缺陷):")
    seen = {}
    for r in rows:
        seen.setdefault(r['tck'], []).append(r['code'])
    for ck, codes in sorted(seen.items()):
        print("      校验和 0x%08X  ← %s" % (ck, "/".join(codes)))
    if len(seen) < 4:
        print("      !! 只有 %d 种校验和 —— 各 profile/模式应给出互不相同的值" % len(seen))
    else:
        print("      ✓ %d 种配置的校验和互不相同 → 哨兵具备可失败性" % len(seen))

    if a.json:
        import json as _json
        out = {
            "scan_flash_addr": sym.get("engine_scan_flash", 0),
            "scan_itcm_addr": sym.get("engine_scan_itcm", 0),
            "scan_size": size.get("engine_scan_flash", 0),
            "isr_addr": sym.get("TIM2_IRQHandler", 0),
            "pad_start": sym.get("_scan_pad_start", 0),
            "pad_end": sym.get("_scan_pad_end", 0),
            "tick_cyc": TICK_CYC,
            "runs": {r['code']: {"eng_min": r['emin'], "eng_max": r['emax'],
                                 "isr_min": r['imin'], "isr_max": r['imax'],
                                 "per_min": r['pmin'], "per_max": r['pmax'],
                                 "sel": r['sel'], "n": r['n'], "prof": r['prof'],
                                 "gate": r['gate'], "tck": r['tck'],
                                 "active": r['active'], "guard": r['guard']}
                     for r in rows},
        }
        try:
            open(a.json, "w", encoding="utf-8").write(_json.dumps(out))
            print("\n  [json] 已写 %s" % a.json)
        except Exception as e:
            print("\n  [json] 写失败: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
