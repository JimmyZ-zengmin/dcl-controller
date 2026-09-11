#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_seq.py — W3 顺序域 (Sequencer) 0x44 SEQ_DEPLOY 的固件侧验收

★ 为什么这是"灵魂测试"而不是"命令测试":
  0x44 的 ACK 只说明"帧被受理了"。真正要证明的是**步进器在运行期按语义推进,
  并驱动译码路由输出** —— 只有"改输入 → 等若干拍 → 读输出"的闭环能证明。

★ 判据源: 全部是 **SHM 本身** (步号、WIRE_MAP), 通过 pyocd 读回 —— 外部工具读
  芯片内真实表, 不是"固件自证"。固件的 g_seq_* 观测面是**第二意见** (交叉验证)。

★ 为什么用 pyocd 而不是串口:
  seq 要验证的是"改完输入等几拍再看输出"的时序。pyocd 直写 SHM 的 WIRE/PARAM 区
  可精确控制输入时刻, 且不需要接线 (本机 CH340 未接, W1 有旁路记录)。

★ 三条命令纪律 (都是本项目踩出来的, 抄错任何一条整个测试就是假绿):
  ① **`go` 之后 sleep 才有意义** —— 否则 pyocd 的 sleep 只是主机侧等待, 核心还
     halt 着, ISR 根本不跑 → "步号不动"会被误读成"seq 坏了"。
  ② **一次 pyocd 连接 = 一次 reset** → main() 跑 cold_start_reset() 清空 SHM。
     所以"从 reset 到要观察的运行期"之间**不能有第二次 reset**; 要观察 A 状态再
     切 B 状态, 必须是**同一条 chain 里 go→sleep→halt→改→go→sleep→halt**。
  ③ **每条 read32 的返回值按命令顺序追加到 vals** ⇒ 每块起始下标 = 它前面所有块
     长度之和, 必须**手算** (不能用"全局字节偏移"模型, 因为同一块可能读两遍)。

★ 布局事实 (必须与 src/engine.h 逐字节一致):
  RouteEntry_t (16B packed):
    @0 src_type @1 src_index @2 dst_type @3 dst_channel @4 op @5 flags
    @6 param_idx(u16) @8 state_offset(u16) @10 actuator_idx(u16)
    @12 wire2_idx(u16) @14 period(u8: div(2bit)+phase(6bit)) @15 reserved
  ParamEntry_t (16B): value_a@0 value_b@4 value_c@8 value_d@12 (4 × f32)
  SeqStepEntry_t (16B): @0 cond_type @1 cond_idx @2 flags @3 reserved
    @4 param_idx(u16) @6 state_offset(u16) @8 jump_idx(u16) @12 reserved2(u32)
  SeqCtrl_t (16B): @0 step_base @2 n_steps @4 step_cur @6 out_wire
    @8 period(u8) @9 run(u8) @10 reserved(u16) @12 step_tick(u32 ★必须最后)

  OP_CMP = 0x01 (★ 不是 0x05 —— 0x05 是 PID); 比较条件在 param.value_b
    (0/默认 = `src > t`); SRC_WIRE = 1, DST_WIRE = 2, ROUTE_FLAG_ACTIVE = 0x01

用法:
    python tools/h723_seq.py            # 全套
    python tools/h723_seq.py --quick    # 只跑 T28
    python tools/h723_seq.py --wipe     # 清持久化 (跑其它回归前必须先做!)
"""

# ★ Windows 控制台默认 GBK: 脚本自己 print 出来的个别字符 (⇒ / ✓ 等) 会以
#   UnicodeEncodeError **直接崩掉整个脚本** —— 数据都量到了, 却崩在"打印结论"这一步,
#   症状看起来像"脚本坏了"而不是"编码问题"。⇒ 统一在入口把 stdout 的错误策略改成
#   "永不抛" (换成 ?), 让验收脚本不可能因为自己的输出而失败。
#   (2026-09-11 实测: audit_m234 / w1 真的这么崩过一次, 整份结果都没打出来。)
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ELF = os.path.join(ROOT, "build", "dcl_h723")

TARGET = "stm32h723xx"
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/"
      "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update"
      ".win32_1.5.0.202011040924/tools/bin/arm-none-eabi-nm.exe")

# ── SHM 偏移 (必须与 src/engine.h 一致) ──
OFF_CTRL_RELOAD     = 0x0C
OFF_CTRL_ENGINE_RUN = 0x0D
OFF_CTRL_N_ROUTES   = 0x0E
OFF_CTRL_N_PARAMS   = 0x10
OFF_CTRL_N_STATES   = 0x12
OFF_CTRL_PROG_MAGIC = 0x14
OFF_CTRL_N_SEQ      = 0x38
OFF_WIRE_MAP        = 0x0240
OFF_ROUTE_TABLE     = 0x0840
OFF_ROUTE_STAGING   = 0x1040
OFF_PARAM_TABLE     = 0x1840
OFF_SEQ_TABLE       = 0x4000
OFF_SEQ_CTRL        = 0x4400
OFF_FORCE_MASK      = 0x47F0

MAX_SEQ_INST = 8

FLASH_BANK1_BASE = 0x08000000
FLASH_SECTOR_SIZE = 0x20000
SEC_A = FLASH_BANK1_BASE + 6 * FLASH_SECTOR_SIZE
SEC_B = FLASH_BANK1_BASE + 7 * FLASH_SECTOR_SIZE

# 原语/枚举
OP_DIRECT = 0x00
OP_CMP   = 0x01
SRC_WIRE = 1
DST_WIRE = 2
ROUTE_FLAG_ACTIVE = 0x01
PERIOD_DIV_MASK = 0x03

# 命令码 (必须与 src/transport.h 一致)
CMD_DEPLOY     = 0x10
CMD_START      = 0x11
CMD_STOP       = 0x12
CMD_RESET      = 0x13
CMD_SEQ_DEPLOY = 0x44

# 程序常量
SRC_W  = 0       # 条件源 wire (测试摇杆, 由测试写)
MIRROR = 5       # seq 步号镜像 wire
DECODE = 6       # 译码输出 wire
TMO_S  = 0.05    # 超时 0.05 秒 (= 500 拍 @ div0 100μs)
WIRE_N = 8       # 观察 WIRE[0..7] 区

RESULTS = []


def record(name, ok, detail="", skip=False):
    RESULTS.append((name, bool(ok), detail, skip))
    tag = "SKIP" if skip else ("PASS" if ok else "FAIL")
    print("  [%s] %-56s %s" % (tag, name, detail))


def nm_syms():
    r = subprocess.run([NM, ELF], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print("!! arm-none-eabi-nm 失败:", r.stderr)
        sys.exit(2)
    syms = {}
    for line in r.stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                syms[p[2]] = int(p[0], 16)
            except ValueError:
                pass
    return syms


LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{4,8}):(.*)$")
HEXWORD_RE = re.compile(r"\b[0-9a-fA-F]{8}\b")
CRIT_RE = re.compile(r"^\d+\s+C\s|^Error:")


def run_chain(cmds, timeout=900, allow_fail=False, verbose=False):
    """pyocd under-reset 命令链 → (vals, raw)。None = 整链作废。

    ★ 与 h723_persist.py 同款: 检查 `C `(critical) / `Error:` / flash 助手超时,
      有则整链判失败 —— 不让"静默半途而废"看起来像"读回不足"。
    ★ vals 的顺序 = 命令顺序 (每条 read32 的输出依次追加)。"""
    chain = []
    for c in cmds:
        chain += ["-c", c]
    cmd = ["pyocd", "cmd", "-t", TARGET, "-O", "connect_mode=under-reset"] + chain + ["-c", "go"]   # ★M4: 收尾 go, 别把核留在 halt
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("!! pyocd 超时 (%d s)" % timeout)
        return None, ""
    raw = r.stdout + r.stderr
    if verbose:
        print(raw[-3000:])
    vals = []
    for line in raw.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        body = m.group(2)
        if "|" in body:
            body = body.split("|")[0]
        vals += [int(x, 16) & 0xFFFFFFFF for x in HEXWORD_RE.findall(body)]
    if not allow_fail:
        for line in raw.splitlines():
            if "flash init timed out" in line:
                print("!! pyocd flash 助手超时 (链中对 flash 地址 write32?)")
                return None, raw
            if CRIT_RE.search(line):
                print("!! pyocd 报错, 整链作废: %s" % line.strip())
                return None, raw
    return vals, raw


def _w32(addr, val):
    """u32 写。★ 非对齐地址必须走 rmw —— pyocd 的 write32 对非对齐地址会**静默
    向低对齐整字写**, 既没写进目标字段又破坏了前一个字段。"""
    assert addr % 4 == 0, "u32 写必须 4 字节对齐: 0x%08X" % addr
    return "write32 0x%08X 0x%08X" % (addr, val & 0xFFFFFFFF)


def _wblock(addr, words):
    """一条 write32 写最多 4 个连续字 (实测多值写正确, 且比多条短)"""
    assert addr % 4 == 0 and 1 <= len(words) <= 4
    return "write32 0x%08X " % addr + " ".join("0x%08X" % (w & 0xFFFFFFFF) for w in words)


def _rmw(addr, cur, shift, mask, value):
    """读-改-写一个对齐字: 只替换 [shift, shift+width) 位段。

    ★ 存在的理由: SHM 头里 RELOAD@0x0C(u8) / ENGINE_RUN@0x0D(u8) / N_ROUTES@0x0E(u16)
      挤在**同一个对齐字**里, 而 N_SEQ@0x38 是独立 u8 (也对齐)。
      直接 write32 非对齐地址 = 静默写错 + 破坏邻字段。"""
    assert addr % 4 == 0
    nv = (cur & ~(mask << shift)) | ((value & mask) << shift)
    return "write32 0x%08X 0x%08X" % (addr, nv & 0xFFFFFFFF)


def rb(base, length):
    """返回 (命令, getter)。getter(vals, start, 第几个字)。
    ★ 索引模型: `read32 <addr> N` 的 N 是**字节数**, 每行印 4 个字, 从 addr 起连续。
      所以调用方必须用"块首下标 + 字序号"取值, 不能按全局字节偏移跳。"""
    assert length > 0 and length % 4 == 0
    return ["read32 0x%08X %d" % (base, length)], (length // 4)


def f32b(v):
    return struct.unpack("<I", struct.pack("<f", v))[0]


def b2f(b):
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


# ══════════════ 表构造 (逐字节对照 engine.h) ══════════════

def rt_route(src_type, src_idx, dst_ch, op, param_idx,
             flags=ROUTE_FLAG_ACTIVE, div=0):
    """RouteEntry_t (16B)"""
    return struct.pack("<BBBBBBHHHHBB",
                       src_type, src_idx, DST_WIRE, dst_ch,
                       op, flags, param_idx, 0, 0, 0,
                       div & PERIOD_DIV_MASK, 0)


def seq_step(cond_type, cond_idx, flags, param_idx):
    """SeqStepEntry_t (16B)。

    ★★ 16 字节的来历 (不是 14): C 结构体是 `packed, aligned(4)`:
         4×u8 + 3×u16 + u32 = 4 + 6 + 4 = **14** 字节 (packed 无内部填充),
         但 `aligned(4)` 把 sizeof 向上取整到 4 的倍数 → **16**。
       末尾 2 字节是**编译器补的尾部填充** (内容不定)。
       ⇒ 本函数的 struct 格式串必须用 `<` **且显式补 2 字节 'xx'**, 否则
         每步少 2 字节 → 3 步 = 42B ≠ 48B → 整个步表错位 (而 C 侧 sizeof 是 16,
         固件按 16 步进 → 读到的是别的步的字节, 看起来像"条件乱判")。
       ★ 这是"两侧结构体尺寸定义法不同"的经典陷阱: C 用 aligned(4) 补齐,
         Python 用 packed 不补齐。判据就是 `len(steps_bytes) == 48` 这条断言 ——
         它当场拦下了这个错 (见本次运行的 AssertionError)。"""
    return struct.pack("<BBBBHHHIxx", cond_type, cond_idx, flags, 0,
                       param_idx, 0, 0, 0)


def seq_frame(entries):
    """entries = [(n_steps, out_wire, div, steps_bytes), ...] → 完整 0x44 载荷"""
    body = struct.pack("<BH", len(entries), sum(e[0] for e in entries))
    off, tbl = 0, b""
    for (ns, ow, dv, steps) in entries:
        body += struct.pack("<BBBBH", ns, ow, dv & PERIOD_DIV_MASK, 0, off)
        tbl += steps
        off += ns
    return body + tbl


def ctrl_words(step_base, n_steps, step_cur, out_wire, period, run, step_tick=0):
    """SeqCtrl_t → 4 个字 (与 seq_step 同款: 小端拼装)"""
    b = struct.pack("<HHHHBBHI", step_base, n_steps, step_cur, out_wire,
                    period, run, 0, step_tick)
    return list(struct.unpack("<4I", b))


# ══════════════ 一个"观察快照" = 一组块读 ══════════════
# ★ 每次观察固定读 4 块, 顺序固定 → 起始下标可手算:
#   [0] WIRE[0..7]      8 字   (off 0x0240)
#   [1] SEQ_CTRL        4 字   (8 实例 × 16B = 128B, 但只读 1 个实例够用 → 读 16B)
#   [2] ctrl(0x0C..0x17) 3 字
#   [3] N_SEQ 字        1 字
NB = (8, 4, 3, 1)


def snapshot_cmds(A):
    return ([rb(A(OFF_WIRE_MAP), WIRE_N * 4)[0][0],
             rb(A(OFF_SEQ_CTRL), 16)[0][0],
             rb(A(OFF_CTRL_RELOAD), 12)[0][0],
             rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0]])


def parse_snapshot(vals, start):
    """→ dict。★ 下标手算: 块长 8/4/3/1"""
    i0 = start
    i1 = i0 + NB[0]
    i2 = i1 + NB[1]
    i3 = i2 + NB[2]
    wires = [b2f(vals[i0 + k]) for k in range(NB[0])]
    c = vals[i1:i1 + 4]
    ctrl = {
        "step_base": c[0] & 0xFFFF,
        "n_steps":   (c[0] >> 16) & 0xFFFF,
        "step_cur":  c[1] & 0xFFFF,
        "out_wire":  (c[1] >> 16) & 0xFFFF,
        "period":    c[2] & 0xFF,
        "run":       (c[2] >> 8) & 0xFF,
        "step_tick": c[3],
    }
    w0 = vals[i2]
    ctl = {
        "reload":   w0 & 0xFF,
        "run":      (w0 >> 8) & 0xFF,
        "n_routes": (w0 >> 16) & 0xFFFF,
    }
    nseq = vals[i3] & 0xFF
    return {"wire": wires, "ctrl": ctrl, "ctl": ctl, "n_seq": nseq}, start + sum(NB)


def wipe():
    vals, raw = run_chain([
        "reset halt", "sleep 300",
        "erase 0x%08X 1" % SEC_A,
        "erase 0x%08X 1" % SEC_B,
        "read32 0x%08X 32" % SEC_A,
        "read32 0x%08X 32" % SEC_B,
        # ★★ 同 persist 的 wipe: **erase 之后 `go` 无效, 必须 `reset` + `go`**
        #    (实测对照)。run_chain 已自动在这个 reset 之后补 `go`。
        "reset",
    ])
    if vals is None or len(vals) < 16:
        print("!! wipe 失败")
        return 1
    ok = all(v == 0xFFFFFFFF for v in vals[0:4]) and all(v == 0xFFFFFFFF for v in vals[4:8])
    print("wipe: SEC_A=0x%08X SEC_B=0x%08X → %s" % (vals[0], vals[8], "OK" if ok else "FAIL"))
    return 0 if ok else 1


# ══════════════════════════════════════════════════════════════════════
# T28 灵魂测试
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# T28 灵魂测试
# ══════════════════════════════════════════════════════════════════════
#
# 程序: 1 个顺序实例 × 3 步, out_wire = WIRE[5], 快档 (div0 = 100μs)
#   step0: cond WIRE[0] > param0.value_a(0.5)          flags=0x00  条件转移
#   step1: cond WIRE[0] > param0.value_a(0.5)          flags=0x00  条件转移
#   step2: cond 无, 仅超时 (ct=2, flags=0x03 = loop|timeout_en)
#   param0: a=0.5 (阈值)  b=0.05 (超时秒 → 500 拍)
#   param1: a=1.5 (比较阈值) b=0 (默认 `src > t`)
# 译码路由: CMP(WIRE[5] > 1.5) → WIRE[6]
#   步号镜像 1.0 起步 ⇒ step0 → 1.0 (不触发) / step1 → 2.0 (触发) / step2 → 3.0 (触发)
#
# ★★ 判据链必须**可失败**, 且每一环配对照:
#   · 段1 步号=0 而 run=1       → 阳性对照: 引擎确实在跑 (不是"因为没跑所以没动")
#   · 段2 步号 0→1 且 DECODE=1  → 条件转移 + 译码闭环 (★核心)
#   · 段3 清条件后步号**不变**  → 阴性对照: 证明段2 的推进**是条件造成的**, 非自走
#   · 段4 再置条件 步号 1→2     → 证明推进可重复 (排除"只动一次"的偶发)
#   · 段5 清条件 + 等超时 → 回 0 → 超时强推 + 末步 loop (★核心)
#   · 段6 STOP 后步号冻结       → 停机语义 (不是清零)

# ══════════════════════════════════════════════════════════════════════
# T28 灵魂测试 — 走**真实协议路径** (0x44 经 proto_dispatch)
# ══════════════════════════════════════════════════════════════════════
#
# 程序:
#   路由: CMP(WIRE[5] > param1.a=1.5) → WIRE[6]   (译码器: 步号≥2 时输出 1.0)
#   seq : 1 实例 × 3 步, out_wire = WIRE[5], 快档 div0 (100μs)
#     step0: cond WIRE[0] > param0.value_a(0.5)   flags=0x00
#     step1: cond WIRE[0] > param2.value_a(3.0)   flags=0x00   ★ 阈值刻意抬高
#     step2: 无条件, 仅超时 (ct=2, flags=0x03 = loop|timeout_en, param0.b=0.05s)
#
#   ★★ 为什么 step0 与 step1 必须用**不同阈值** (第一版踩的坑):
#     若两步同一条件 (都 >0.5), 则 WIRE[0]=2.0 时 step0→step1 后 step1 条件**立刻**
#     又成立 ⇒ 一次观察窗口内连跳到 step2。症状: 段1 读回 step_cur=2 而非 1,
#     看起来像"引擎多跳了一步"(假故障), 实则是**测试用例没有分辨力** ——
#     它根本区分不出"推进一次"与"推进两次"。
#     ⇒ 阈值错开 (0.5 / 3.0) 之后:
#         WIRE[0]=2.0 → 只满足 step0 ⇒ 停在 step1   (可分辨"推进了一次")
#         WIRE[0]=0.0 → 两个都不满足 ⇒ 冻结        (阴性对照)
#         WIRE[0]=4.0 → 满足 step1   ⇒ 停在 step2   (推进可重复)
#       这才是"每步的转移条件都可被独立驱动"的可失败判据。
#
#   步号镜像 1.0 起步 ⇒ step0→1.0 (译码不触发) / step1→2.0 (触发) / step2→3.0 (触发)
#
# ★★ 判据链 (每环都能失败, 且配对照):
#   ① 3 条命令 (0x10/0x12/0x44) 都被真实分发 (阳性对照)
#   ② START 后 step0 停留, W5=1.0, W6=0.0  ← ★含"镜像初值"判据 (抓移植遗漏)
#   ③ 置 WIRE[0]=2.0 → 停在 step1, W5=2.0, W6=1.0   ← ★条件转移 + 译码闭环
#   ④ 清 WIRE[0]=0.0 → 步号冻结在 1        ← ★阴性对照 (证明③是条件造成的)
#   ⑤ 置 WIRE[0]=4.0 → 停在 step2, W5=3.0   ← 推进可重复
#   ⑥ 清条件 + 等超时 0.05s → 回卷 step0, W5=1.0, W6=0.0  ← ★超时强推 + 末步 loop
#   ⑦ STOP 后步号冻结 (不是清零)
#   ⑧ NAK 用例 (校验器) 见 t29_nak
#
# ★★★ 三条"为什么这样写" (每条都是踩出来的, 改之前先读):
#   [甲] 引擎扫的是 **桶表** (OFF_ROUTE_BUCKETS), 而桶表由 deploy 路径里的
#        engine_build_buckets 产出。**直写路由表对引擎不可见** —— 实测症状是
#        "路由表内容看起来对, 但引擎输出的是上一个程序的值"。所以程序必须经
#        真实的 0x10/0x44 命令装载。
#   [乙] 一次 pyocd 连接 = 一次 reset; `go` 会让 main() 从头上电流程跑一遍
#        (cold_start_reset 清 SHM + engine_fill_tables 填 profile)。
#        ⇒ 正确时序: reset → go+sleep (让 boot 跑完) → halt → 发命令 → go+观察。
#        中间的 halt **不是 reset**, 写入与已装载的程序都能存活。
#   [丙] 免串口发帧: 把裸载荷写进 SHM 尾部 (OFF_MB_TAIL), 置 g_cmd_req_len/g_cmd_req,
#        主循环交给**真实的 proto_dispatch** —— 命令分发/校验器/ACK-NAK 全被走到。

# 免串口协议帧暂存区偏移 —— 必须与 src/engine.h 的 OFF_CMD_REQ 一致。
# ★ W4 起从 0x4B20 改为 0x4DE0: W3 时它落在 0x4B20, 而 W4 的 MB_CTRL 要用那一段
#   (通信域五段现在占 0x4AA0..0x4DE0), 暂存区随之后移到 MB_END 之后。
MB_TAIL_OFF = 0x4DE0


def _stage_cmd(stg, cmd, payload=b""):
    """把一条命令 (cmd + payload) 写进 SHM 尾部暂存区, 并布置触发标志。

    ★ 返回命令列表 (不含 go/触发), 触发由调用方在"观察窗口"里一起发。
    ★ 为什么 seg 写而不是整帧写: 载荷最长 ~3KB, 用 4 字一条的 write32 要几百条;
      但 pyocd 的 write32 支持多值 (实测正确), 一次可写很多字 —— 这里按 64 字/条
      分批 (实测 4 字稳; 保守取 32 字/条, 兼顾条数)。
    """
    blob = bytes([cmd]) + payload
    c = []
    # 32B 对齐: 暂存区起点按 4 对齐, 按字节展开成字
    pad = (-len(blob)) % 4
    blob4 = blob + b"\x00" * pad
    words = struct.unpack("<%dI" % (len(blob4) // 4), blob4)
    for k in range(0, len(words), 32):
        chunk = words[k:k + 32]
        c.append("write32 0x%08X " % (stg + k * 4)
                 + " ".join("0x%08X" % w for w in chunk))
    c.append(_w32(syms_ref[0]["g_cmd_req_len"], len(blob)))
    c.append(_w32(syms_ref[0]["g_cmd_req"], 1))
    return c


syms_ref = [None]     # 由 t28 填 (避免到处传 syms)


def t28(syms, A, observe_ms=40, timeout_ms=900, BOOT_MS=250):
    syms_ref[0] = syms
    print("─" * 80)
    print("T28: 条件转移 / 超时强推 / 末步 loop / 步号镜像 / 停机冻结 (走真实协议路径)")
    print("─" * 80)

    STG = A(MB_TAIL_OFF)

    # ── 程序构造 ──
    # 路由: CMP(WIRE[5] > param1.a=1.5) → WIRE[6], div0
    # ★ OP_CMP 的默认分支 (value_b=0) 是 `src > t`, 所以 param1.a 就是比较阈值
    RT = rt_route(SRC_WIRE, MIRROR, DECODE, OP_CMP, param_idx=1, div=0)
    # param 表 (3 条):
    #   param0: a=0.5 (step0 阈值)  b=TMO_S (step2 超时秒)
    #   param1: a=1.5 (路由比较阈值) b=0
    #   param2: a=3.0 (step1 阈值, ★ 刻意高于 param0 —— 见文件头"为什么必须不同")
    P0 = struct.pack("<4f", 0.5, TMO_S, 0.0, 0.0)
    P1 = struct.pack("<4f", 1.5, 0.0, 0.0, 0.0)
    P2 = struct.pack("<4f", 3.0, 0.0, 0.0, 0.0)
    deploy_payload = struct.pack("<HHH", 1, 3, 0) + RT + P0 + P1 + P2

    # seq 程序: 3 步 (step0 用 param0, step1 用 param2, step2 超时用 param0.b)
    steps_bytes = (seq_step(1, SRC_W, 0x00, 0) +      # step0: WIRE[0] > 0.5
                   seq_step(1, SRC_W, 0x00, 2) +      # step1: WIRE[0] > 3.0
                   seq_step(2, 0,     0x03, 0))       # step2: 纯超时 + loop
    seq_payload = seq_frame([(3, MIRROR, 0, steps_bytes)])
    assert len(steps_bytes) == 48
    assert len(seq_payload) == 3 + 6 + 48, len(seq_payload)

    # ── 链构造 ──
    # [甲][乙] 见注释块。第一阶段: reset → go (boot 跑完) → halt
    cmds = ["reset halt", "sleep 300", "go", "sleep %d" % BOOT_MS, "halt"]

    # ① 装载路由程序 (0x10): 触发 → go → halt
    cmds += _stage_cmd(STG, CMD_DEPLOY, deploy_payload)
    cmds += ["go", "sleep 80", "halt",
             rb(A(OFF_CTRL_RELOAD), 12)[0][0],          # ctl 字 (含 n_routes/run)
             rb(A(OFF_ROUTE_TABLE), 16)[0][0]]          # 第 0 条路由 (核对装载)

    # ② 装载 seq 程序 (0x44)。★ 先 STOP (0x44 要求 STOP 态)
    cmds += _stage_cmd(STG, CMD_STOP)
    cmds += ["go", "sleep 40", "halt"]
    cmds += _stage_cmd(STG, CMD_SEQ_DEPLOY, seq_payload)
    cmds += ["go", "sleep 60", "halt",
             rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0],       # N_SEQ 字
             rb(A(OFF_SEQ_CTRL), 16)[0][0]]             # 实例 0 控制块

    # ③ START (0x11) → 引擎 RUN (seq 的 run 位由固件在这次 START 里置起)
    cmds += _stage_cmd(STG, CMD_START)
    cmds += ["go", "sleep 40", "halt"]

    # ── 观察窗口 ──
    N_WIN = sum(NB)      # 16 字/窗口

    def win(pre=None, ms=observe_ms):
        out = list(pre or [])
        out += ["go", "sleep %d" % ms, "halt"]
        out += snapshot_cmds(A)
        return out

    cmds += win(ms=observe_ms)                                            # 段0: step0
    cmds += win(pre=[_w32(A(OFF_WIRE_MAP) + SRC_W * 4, f32b(2.0))])        # 段1: →step1
    cmds += win(pre=[_w32(A(OFF_WIRE_MAP) + SRC_W * 4, f32b(0.0))])        # 段2: 冻结
    cmds += win(pre=[_w32(A(OFF_WIRE_MAP) + SRC_W * 4, f32b(4.0))])        # 段3: →step2
    cmds += win(pre=[_w32(A(OFF_WIRE_MAP) + SRC_W * 4, f32b(0.0))],
                ms=timeout_ms)                                            # 段4: 超时回卷
    # 段5: STOP → 步号冻结
    #   ★ 第一版这里**漏发了 STOP 命令** (只 go/sleep/halt), 于是 ENGINE_RUN 仍是 1,
    #     判据读回 run=1 → FAIL。教训: "停机冻结"这条判据必须有**真正的停机动作**
    #     在它之前, 否则测的是"什么都没发生" —— 而"没发生"与"冻结住了"在数据上
    #     完全一样 (这正是本项目"判据必须能失败"要防的那类空判据)。
    cmds += _stage_cmd(STG, CMD_STOP)
    cmds += ["go", "sleep %d" % observe_ms, "halt",
             rb(A(OFF_CTRL_RELOAD), 12)[0][0],
             rb(A(OFF_SEQ_CTRL), 16)[0][0]]
    i_stopctl = None   # 见下面的下标手算

    # 观测面
    obs = ["g_seq_ticks", "g_seq_deploys", "g_seq_writes", "g_seq_steps_sum",
           "g_seq_max_cur", "g_seq_last_cur", "g_seq_nak", "g_seq_armed",
           "g_cmd_req_cnt", "g_cmd_req_last"]
    for s in obs:
        cmds.append("read32 0x%08X 4" % syms[s])
    cmds.append("read32 0x%08X 4" % syms["g_eng_ticks"])

    # ── 执行 ──
    vals, raw = run_chain(cmds, timeout=300)
    if vals is None:
        record("T28 链执行", False, "pyocd 整链作废")
        print(raw[-1500:])
        return False

    # ── 下标手算 ──
    # ★★ 纪律: `run_chain` 的 vals 只包含 read32 的输出, **按命令顺序依次追加**。
    #    所以必须精确知道"哪几条 read32、各回几个字"。
    #    本 chain 的 read32 只有这些 (写命令/go/sleep/halt 不贡献):
    #      [A] 0x10 后: rb(RELOAD,12) → 3 字
    #      [B] 0x10 后: rb(ROUTE_TABLE,16) → 4 字
    #      [C] 0x44 后: rb(N_SEQ&~3,4) → 1 字
    #      [D] 0x44 后: rb(SEQ_CTRL,16) → 4 字
    #      [E] 5 个观察窗口 × snapshot_cmds (8+4+3+1 = 16 字)  → 80 字
    #      [F] 段5 STOP 后: rb(RELOAD,12)=3 字 + rb(SEQ_CTRL,16)=4 字 → 7 字
    #      [G] obs 10 字 + g_eng_ticks 1 字
    i = 0
    i_ctl10 = i; i += 3          # [A]
    i_rt    = i; i += 4          # [B]
    i_nsq   = i; i += 1          # [C]
    i_sc    = i; i += 4          # [D]
    win_base = [i + k * N_WIN for k in range(5)]
    i += 5 * N_WIN               # [E]
    i_s5ctl = i                  # [F]
    i_s5sc  = i + 3
    i += 3 + 4
    i_obs   = i                  # [G]
    i_eng   = i_obs + len(obs)
    total   = i_eng + 1

    if len(vals) < total:
        record("T28 读回完整", False, "期望 %d 字, 实得 %d" % (total, len(vals)))
        print(raw[-1200:])
        return False

    ctl10_0 = vals[i_ctl10 + 0]
    rt0 = vals[i_rt:i_rt + 4]
    nsq_word = vals[i_nsq]
    sc = vals[i_sc:i_sc + 4]
    snaps = [parse_snapshot(vals, win_base[k])[0] for k in range(5)]
    s5_ctl = vals[i_s5ctl]
    s5_sc = vals[i_s5sc:i_s5sc + 4]
    o = {s: vals[i_obs + j] for j, s in enumerate(obs)}
    eng_ticks = vals[i_eng]

    ctl10 = {
        "reload": ctl10_0 & 0xFF,
        "run": (ctl10_0 >> 8) & 0xFF,
        "n_routes": (ctl10_0 >> 16) & 0xFFFF,
    }
    sc0 = {
        "step_cur": sc[1] & 0xFFFF,
        "out_wire": (sc[1] >> 16) & 0xFFFF,
        "run": (sc[2] >> 8) & 0xFF,
        "n_steps": (sc[0] >> 16) & 0xFFFF,
    }
    s5ctrl = {
        "run": (s5_ctl >> 8) & 0xFF,
        "step_cur": s5_sc[1] & 0xFFFF,
    }

    # ── 打印 ──
    print("  0x10 后: N_ROUTES=%d ENGINE_RUN=%d  route0=0x%08X,%08X,%08X,%08X"
          % (ctl10["n_routes"], ctl10["run"], rt0[0], rt0[1], rt0[2], rt0[3]))
    print("  0x44 后: N_SEQ=%d  实例0: n_steps=%d step_cur=%d out_wire=%d run=%d"
          % (nsq_word & 0xFF, sc0["n_steps"], sc0["step_cur"], sc0["out_wire"], sc0["run"]))
    print("  引擎拍数 g_eng_ticks=%d  免串口帧受理 g_cmd_req_cnt=%d last=0x%02X"
          % (eng_ticks, o["g_cmd_req_cnt"], o["g_cmd_req_last"]))
    print("  g_seq_ticks=%d deploys=%d writes=%d steps_sum=%d max_cur=%d nak=%d armed=%d"
          % (o["g_seq_ticks"], o["g_seq_deploys"], o["g_seq_writes"], o["g_seq_steps_sum"],
             o["g_seq_max_cur"], o["g_seq_nak"], o["g_seq_armed"]))
    print()
    print("  %-4s %-8s %-9s %-8s %-8s %-8s" % ("段", "step_cur", "step_tick", "run", "W5镜像", "W6译码"))
    for k, s in enumerate(snaps):
        print("  %-4d %-8d %-9d %-8d %-8.2f %-8.2f"
              % (k, s["ctrl"]["step_cur"], s["ctrl"]["step_tick"], s["ctrl"]["run"],
                 s["wire"][MIRROR], s["wire"][DECODE]))
    print("  STOP 后: ENGINE_RUN=%d step_cur=%d" % (s5ctrl["run"], s5ctrl["step_cur"]))
    print()

    # ── 判据 ──
    s0, s1, s2, s3, s4 = snaps

    record("T28.A1 ★阳性对照: 3 条免串口命令(0x10/0x12/0x44)都被真实分发",
           o["g_cmd_req_cnt"] >= 4,
           "g_cmd_req_cnt=%d last=0x%02X" % (o["g_cmd_req_cnt"], o["g_cmd_req_last"]))

    record("T28.A2 0x10 装载: N_ROUTES=1, route0.dst=WIRE[6]",
           ctl10["n_routes"] == 1 and (rt0[0] >> 24) & 0xFF == DECODE,
           "N_ROUTES=%d dst=%d" % (ctl10["n_routes"], (rt0[0] >> 24) & 0xFF))

    record("T28.A3 ★0x44 受理: N_SEQ=1, 实例0 n_steps=3 out_wire=5, run 已由 START 置 1",
           (nsq_word & 0xFF) == 1 and sc0["n_steps"] == 3 and sc0["out_wire"] == MIRROR
           and o["g_seq_deploys"] == 1,
           "N_SEQ=%d n_steps=%d out_wire=%d deploys=%d" % (nsq_word & 0xFF, sc0["n_steps"],
                                                            sc0["out_wire"], o["g_seq_deploys"]))

    record("T28.B0 ★阳性对照: seq 段每拍都被调用 (g_seq_ticks > 0) 且引擎在跑 (eng_ticks > 0)",
           o["g_seq_ticks"] > 0 and eng_ticks > 0,
           "g_seq_ticks=%d eng_ticks=%d" % (o["g_seq_ticks"], eng_ticks))

    record("T28.B1 ★起点快照: START 后停在 step0, 镜像已置 1.0, 译码 0.0 "
           "(抓出移植遗漏: 第一版此处读到上一程序残值 0.01)",
           s0["ctrl"]["step_cur"] == 0 and abs(s0["wire"][MIRROR] - 1.0) < 1e-6
           and abs(s0["wire"][DECODE]) < 1e-6,
           "cur=%d W5=%.2f W6=%.2f" % (s0["ctrl"]["step_cur"], s0["wire"][MIRROR], s0["wire"][DECODE]))

    record("T28.B2 ★arm 计数: g_seq_armed == 1 (START 真的走到 seq arm 段)",
           o["g_seq_armed"] == 1, "g_seq_armed=%d" % o["g_seq_armed"])

    record("T28.C1 ★核心: WIRE0=2.0 满足 step0(>0.5) 但不满足 step1(>3.0) → 停在 step1, "
           "镜像=2.0, 译码=1.0",
           s1["ctrl"]["step_cur"] == 1 and abs(s1["wire"][MIRROR] - 2.0) < 1e-6
           and abs(s1["wire"][DECODE] - 1.0) < 1e-6,
           "cur=%d W5=%.2f W6=%.2f" % (s1["ctrl"]["step_cur"], s1["wire"][MIRROR], s1["wire"][DECODE]))

    record("T28.C2 ★阴性对照: 清条件(WIRE0=0.0) → 步号冻结在 1 (证明 C1 的推进由条件造成, 非自走)",
           s2["ctrl"]["step_cur"] == 1 and abs(s2["wire"][MIRROR] - 2.0) < 1e-6,
           "cur=%d W5=%.2f" % (s2["ctrl"]["step_cur"], s2["wire"][MIRROR]))

    record("T28.C3 WIRE0=4.0 满足 step1(>3.0) → 停在 step2, 镜像=3.0 (推进可重复)",
           s3["ctrl"]["step_cur"] == 2 and abs(s3["wire"][MIRROR] - 3.0) < 1e-6
           and abs(s3["wire"][DECODE] - 1.0) < 1e-6,
           "cur=%d W5=%.2f W6=%.2f" % (s3["ctrl"]["step_cur"], s3["wire"][MIRROR], s3["wire"][DECODE]))

    record("T28.D1 ★核心: 末步无条件下等超时 %.2fs → 回卷 step0, 镜像=1.0, 译码=0.0" % TMO_S,
           s4["ctrl"]["step_cur"] == 0 and abs(s4["wire"][MIRROR] - 1.0) < 1e-6
           and abs(s4["wire"][DECODE]) < 1e-6,
           "cur=%d W5=%.2f W6=%.2f" % (s4["ctrl"]["step_cur"], s4["wire"][MIRROR], s4["wire"][DECODE]))

    record("T28.D2 历史最大步号 = 2 (证明真的走到过末步, 非中途卡住)",
           o["g_seq_max_cur"] == 2, "g_seq_max_cur=%d" % o["g_seq_max_cur"])

    record("T28.E1 停机冻结: STOP 后 ENGINE_RUN=0 且步号保持 (不是清零)",
           s5ctrl["run"] == 0 and s5ctrl["step_cur"] == s4["ctrl"]["step_cur"],
           "run=%d cur=%d (STOP 前 %d)" % (s5ctrl["run"], s5ctrl["step_cur"], s4["ctrl"]["step_cur"]))

    record("T28.E2 交叉验证: g_seq_writes>0 且 与 steps_sum 非零 (真的写了镜像)",
           o["g_seq_writes"] > 0 and o["g_seq_steps_sum"] > 0,
           "writes=%d steps_sum=%d" % (o["g_seq_writes"], o["g_seq_steps_sum"]))

    record("T28.F0 本测试的命令全部合法 → g_seq_nak 应为 0",
           o["g_seq_nak"] == 0, "g_seq_nak=%d" % o["g_seq_nak"])
    return True


def t29_nak(syms, A, BOOT_MS=250):
    """T29 — 0x44 **校验器** 的 NAK 用例 (全部走真实 0x44 处理器)。

    ★ 为什么 NAK 用例和 T28 一样重要: 校验器是"下载前静态校验"这个卖点的本体。
      只测"合法程序能装"证明不了校验器存在 —— 必须测"非法程序**被拒**且**原因正确**"。
      每个用例都同时核对: ① 确实 NAK 了 (g_seq_nak 增加) ② N_SEQ **没有被改**
      (拒收 = 不留半成品, 这是"下载前校验"的全部意义)。
    """
    syms_ref[0] = syms
    print()
    print("─" * 80)
    print("T29: 0x44 校验器 NAK 用例 (走真实 0x44 处理器)")
    print("─" * 80)

    STG = A(MB_TAIL_OFF)

    # ★★ 前置条件必须**显式构造**, 不能依赖 boot profile 的副作用 —— 第一版这里
    #    踩了: T29.0 "合法帧" 被拒, 原因不是帧不合法, 而是 **boot profile 填了
    #    128 条路由 (dst_channel = i%128) 占满全部 wire**, 于是 out_wire=5 必然
    #    撞上某条路由的 dst → OA3 "out_wire conflicts writer" 拒收。
    #    那是"校验器正确地拒了一个在本环境下必然冲突的帧", 而测试却把它当成
    #    "合法帧被误拒"。⇒ 教训: 判据的前置条件若来自被测系统之外 (这里是 boot
    #    默认程序), 它就**不是可控变量**, 必须显式清成已知状态。
    #    做法: 先发一条 nr=0 的 0x10 (清空 ACTIVE 路由), 把 wire 空间腾出来。
    #    ★★ 第二处坑 (T29.0 修了一轮才过): param 表也必须**合法**。第一版这里写
    #       param0 = (1.0, 0.0, ...) —— 而 good_frame 的 step2 带 timeout_en, 校验器
    #       会查 `param[param_idx].value_b > 0` (超时秒必须为正), 于是 0.0 被拒。
    #       也就是说: 那个 FAIL **又是校验器正确工作**, 而我的"阳性对照"数据本身
    #       违规。⇒ 阳性对照的载荷必须逐条满足**全部**校验规则, 否则测的是
    #       "校验器拒了非法数据", 却当成"合法数据被误拒"。
    #       正确做法: param0.b = 0.05 (合法超时秒), param0.a = 1.0 (合法阈值)。
    CLEAR_ROUTES = struct.pack("<HHH", 0, 1, 0) + struct.pack("<4f", 1.0, 0.05, 0.0, 0.0)

    def good_frame():
        st = (seq_step(1, SRC_W, 0x00, 0) +
              seq_step(1, SRC_W, 0x00, 0) +
              seq_step(2, 0, 0x03, 0))
        return seq_frame([(3, MIRROR, 0, st)])

    def one_case(name, payload, note=""):
        """发一条 0x44 → 读 g_seq_nak/N_SEQ。返回 (nak_delta, N_SEQ_after)。"""
        c = ["reset halt", "sleep 300", "go", "sleep %d" % BOOT_MS, "halt"]
        # ① 先 STOP (0x44 要求 STOP 态), 否则所有用例都因 RUN 被拒 → 失去分辨力
        c += _stage_cmd(STG, CMD_STOP); c += ["go", "sleep 40", "halt"]
        # ② 清空路由 (腾出 wire 空间, 见上面 CLEAR_ROUTES 的说明)
        c += _stage_cmd(STG, CMD_DEPLOY, CLEAR_ROUTES); c += ["go", "sleep 60", "halt"]
        c.append(rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0])            # 基线 N_SEQ 字
        c.append("read32 0x%08X 4" % syms["g_seq_nak"])          # 基线 nak
        c += _stage_cmd(STG, CMD_SEQ_DEPLOY, payload)
        c += ["go", "sleep 60", "halt"]
        c.append("read32 0x%08X 4" % syms["g_seq_nak"])          # 后 nak
        c.append(rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0])            # 后 N_SEQ
        vals, raw = run_chain(c, timeout=150)
        if vals is None or len(vals) < 4:
            record(name, False, "链失败", skip=True)
            return None, None
        nak0, nak1 = vals[1], vals[2]
        nseq0, nseq1 = vals[0] & 0xFF, vals[3] & 0xFF
        ok = (nak1 > nak0) and (nseq1 == nseq0)
        record(name, ok, "nak %d→%d, N_SEQ %d→%d %s" % (nak0, nak1, nseq0, nseq1, note))
        return nak1 > nak0, nseq1

    # ── 用例 ──
    # ① 基线阳性对照: 合法帧必须**不**被拒 (此时路由已清空)
    c = ["reset halt", "sleep 300", "go", "sleep %d" % BOOT_MS, "halt"]
    c += _stage_cmd(STG, CMD_STOP); c += ["go", "sleep 40", "halt"]
    c += _stage_cmd(STG, CMD_DEPLOY, CLEAR_ROUTES); c += ["go", "sleep 60", "halt"]
    c.append("read32 0x%08X 4" % syms["g_seq_nak"])
    c += _stage_cmd(STG, CMD_SEQ_DEPLOY, good_frame())
    c += ["go", "sleep 60", "halt"]
    c.append("read32 0x%08X 4" % syms["g_seq_nak"])
    c.append(rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0])
    vals, raw = run_chain(c, timeout=150)
    if vals and len(vals) >= 3:
        record("T29.0 ★阳性对照: 合法帧被受理 (nak 不变, N_SEQ=1)",
               vals[1] == vals[0] and (vals[2] & 0xFF) == 1,
               "nak %d→%d N_SEQ=%d" % (vals[0], vals[1], vals[2] & 0xFF))
    else:
        record("T29.0 ★阳性对照: 合法帧被受理", False, "链失败", skip=True)

    # ② 帧长不符 (多 1 字节)
    f = good_frame()
    one_case("T29.1 帧长不符 (多 1 字节) → 拒收", f + b"\x00", "长度校验")

    # ③ n_seq = 0
    one_case("T29.2 n_seq=0 → 拒收", struct.pack("<BH", 0, 3), "n_seq 下界")

    # ④ n_seq 越界 (9 > MAX_SEQ_INST=8)
    one_case("T29.3 n_seq=9 (> MAX_SEQ_INST=8) → 拒收", struct.pack("<BH", 9, 3), "n_seq 上界")

    # ⑤ out_wire = 0 (哨兵: 保留给"无镜像")
    st = seq_step(2, 0, 0x03, 0)
    one_case("T29.4 out_wire=0 (保留哨兵) → 拒收",
             seq_frame([(1, 0, 0, st)]), "OA3 哨兵")

    # ⑥ 纯超时步缺 timeout_en (OA6)
    st = seq_step(2, 0, 0x01, 0)      # 只有 loop, 没有 timeout_en
    one_case("T29.5 ★OA6 纯超时步缺 timeout_en → 拒收 (否则永久卡步)",
             seq_frame([(1, MIRROR, 0, st)]), "OA6 卡步防护")

    # ⑦ param_idx 越界 (128 = MAX_PARAMS)
    st = seq_step(1, 0, 0x00, 128)
    one_case("T29.6 param_idx=128 (>= MAX_PARAMS) → 拒收",
             seq_frame([(1, MIRROR, 0, st)]), "param_idx 边界")

    # ⑧ cond_type 非法 (>2)
    st = seq_step(3, 0, 0x00, 0)
    one_case("T29.7 cond_type=3 (> 2) → 拒收",
             seq_frame([(1, MIRROR, 0, st)]), "cond_type 边界")

    # ⑨ div 非法 (3 > PERIOD_DIV_IDX_SLOW=2)
    st = seq_step(2, 0, 0x03, 0)
    one_case("T29.8 div=3 (非法档位) → 拒收",
             seq_frame([(1, MIRROR, 3, st)]), "div 边界")

    # ⑩ step_off 不连续 (声明 5, 实际 0)
    st = seq_step(2, 0, 0x03, 0)
    body = struct.pack("<BH", 1, 1) + struct.pack("<BBBBH", 1, MIRROR, 0, 0, 5) + st
    one_case("T29.9 step_off 不连续 (声明 5, 应为 0) → 拒收", body, "结构一致性")

    # ⑪ 目录步数与实际不符 (声明 1, 总步数 2)
    st = seq_step(2, 0, 0x03, 0)
    body = struct.pack("<BH", 1, 2) + struct.pack("<BBBBH", 1, MIRROR, 0, 0, 0) + st
    one_case("T29.10 目录步数与总步数不符 (1 vs 2) → 拒收", body, "dir/total 一致性")

    # ⑫ ★ OA3: out_wire 撞路由 dst
    c = ["reset halt", "sleep 300", "go", "sleep %d" % BOOT_MS, "halt"]
    # 先装一条 dst=WIRE[5] 的路由 (占住 MIRROR)
    RTc = rt_route(SRC_WIRE, 0, MIRROR, OP_DIRECT, param_idx=0, div=0)
    dpay = struct.pack("<HHH", 1, 1, 0) + RTc + struct.pack("<4f", 0, 0, 0, 0)
    c += _stage_cmd(STG, CMD_STOP); c += ["go", "sleep 40", "halt"]
    c += _stage_cmd(STG, CMD_DEPLOY, dpay); c += ["go", "sleep 60", "halt"]
    c.append("read32 0x%08X 4" % syms["g_seq_nak"])
    # 再发一个 out_wire=5 的 seq → OA3 应撞上 → 拒收
    st = seq_step(2, 0, 0x03, 0)
    c += _stage_cmd(STG, CMD_SEQ_DEPLOY, seq_frame([(1, MIRROR, 0, st)]))
    c += ["go", "sleep 60", "halt"]
    c.append("read32 0x%08X 4" % syms["g_seq_nak"])
    c.append(rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0])
    vals, raw = run_chain(c, timeout=150)
    if vals and len(vals) >= 3:
        record("T29.11 ★OA3 out_wire 与路由 dst 撞槽 → 拒收 (否则镜像被每拍覆盖)",
               vals[1] > vals[0] and (vals[2] & 0xFF) == 0,
               "nak %d→%d N_SEQ=%d" % (vals[0], vals[1], vals[2] & 0xFF))

    # ⑬ RUN 态部署必须被拒 (seq 无 staging, 半写表 = 不可归因)
    c = ["reset halt", "sleep 300", "go", "sleep %d" % BOOT_MS, "halt"]
    c += _stage_cmd(STG, CMD_START); c += ["go", "sleep 40", "halt"]
    c.append("read32 0x%08X 4" % syms["g_seq_nak"])
    st = seq_step(2, 0, 0x03, 0)
    c += _stage_cmd(STG, CMD_SEQ_DEPLOY, seq_frame([(1, MIRROR, 0, st)]))
    c += ["go", "sleep 60", "halt"]
    c.append("read32 0x%08X 4" % syms["g_seq_nak"])
    c.append(rb(A(OFF_CTRL_N_SEQ) & ~3, 4)[0][0])
    vals, raw = run_chain(c, timeout=150)
    if vals and len(vals) >= 3:
        record("T29.12 ★OA7 RUN 态部署 → 拒收 (必须 STOP 态)",
               vals[1] > vals[0] and (vals[2] & 0xFF) == 0,
               "nak %d→%d N_SEQ=%d" % (vals[0], vals[1], vals[2] & 0xFF))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    if args.wipe:
        return wipe()

    syms = nm_syms()
    need = ["g_shm", "g_seq_deploys", "g_seq_nak", "g_seq_writes", "g_seq_steps_sum",
            "g_seq_ticks", "g_seq_max_cur", "g_seq_last_cur", "g_seq_armed",
            "g_cmd_req", "g_cmd_req_len", "g_cmd_req_cnt", "g_cmd_req_last",
            "g_eng_ticks"]
    missing = [s for s in need if s not in syms]
    if missing:
        print("!! 符号缺失:", missing, "\n   (是不是没加进 obs_anchor?)")
        return 2

    SHM = syms["g_shm"]
    A = lambda off: SHM + off
    print("=" * 80)
    print("W3 Sequencer 0x44 — 固件侧验收")
    print("=" * 80)
    print("  g_shm=0x%08X  SEQ_TABLE=0x%08X  SEQ_CTRL=0x%08X"
          % (SHM, A(OFF_SEQ_TABLE), A(OFF_SEQ_CTRL)))
    print("  ★ 跑前必须已 wipe 持久化配置 (否则上电走 persist 恢复, 不走 bench 默认)")
    print()

    t28(syms, A)
    if not args.quick:
        t29_nak(syms, A)

    print()
    print("=" * 80)
    npass = sum(1 for _, ok, _, sk in RESULTS if ok and not sk)
    nfail = sum(1 for _, ok, _, sk in RESULTS if not ok and not sk)
    nskip = sum(1 for _, ok, _, sk in RESULTS if sk)
    print("结果: %d PASS / %d FAIL / %d SKIP / 共 %d"
          % (npass, nfail, nskip, len(RESULTS)))
    print("=" * 80)
    return 0 if (nfail == 0 and npass > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
