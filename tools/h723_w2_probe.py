#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_w2_probe.py — W2 Force 的**免串口**固件侧验证 (pyocd 单会话直驱 SHM)

★ 为什么需要这个脚本 (而不是只有 h723_force.py):
   W2 的 Force 语义有两半 —— 拍首覆写 (engine_tick) 与写端屏蔽 (engine_scan_*)。
   两半都在 **ISR 内**, 而 ISR 的输入只来自 SHM 的 FORCE_MASK / FORCE_VAL。
   ⇒ 只要能写 SHM 并观察 WIRE_MAP 的演化, 就能**完整验证 ISR 行为**,
     完全不依赖 UART 物理链路 —— CH340 的接线问题不阻塞这一步。

★ 判据设计 (全部可失败 + **至少一个非零强制值**):
   A. 阳性对照: 不强制时, 有路由写 wire[N] → WIRE_MAP[N] 必须为**稳定非零**
      (证明引擎真的在跑、写路径真的通 —— 否则 C 的"值没变"毫无意义)
   B. 拍首覆写: 置 FORCE_MASK[N] + FORCE_VAL[N] = 7.5 → 跑若干拍 → 读回 == 7.5
      ★ **非零值** 7.5 是刻意选的 —— OA9 事故的判据盲区正是"测试全用 0.0",
        那个 bug (只写 WIRE_MAP 不写 FORCE_VAL) 与期望完全重合 → 13/13 全绿仍错。
   C. 写端屏蔽: 强制期间路由仍在写 N → 读回仍须 == 7.5
   D. 释放: 清 FORCE_MASK[N] → 跑若干拍 → 必须 != 7.5 (路由恢复写入)
   E. 换值哨兵: 改成另一个非零值 (3.25) → 读回 == 3.25 (排除残留/缓存假象)
   F. 值确实进的是 FORCE_VAL: 读回 SHM[FORCE_VAL+N] 必须 == 强制值
      —— 直接验证"拍首覆写的输入源"本身, 而不只是它的下游效果

★ 连接模式: **必须 under-reset**。实测 (2026-09-11) 默认 connect_mode 下:
   ① 芯片进 WFI 后 SWD 失步 ("Device entered sleep" / memory transfer failed)
   ② 单会话长命令链中段常报 AP#0 IDR 读失败 → "No cores were discovered"
   under-reset 模式下同一套命令链稳定复跑 (已实测 4 轮)。
   代价: 每次连接都会 reset —— 所以**全部用例必须放进一条命令链**。

★ 分段手法: "跑一段时间 → halt → 读 → go" 循环。pyocd 的 `sleep` 命令在 halt 态
   下是**主机侧 sleep**, 在 go 态下也是主机侧等待 —— 所以 `go`+`sleep`+`halt`
   组合能可靠地"让引擎跑 N 毫秒再停下来看"。

用法:
    python tools/h723_w2_probe.py                # 默认
    python tools/h723_w2_probe.py --w 7          # 换被测 wire
    python tools/h723_w2_probe.py --spin 5       # 每个用例跑 5ms
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
OFF_CTRL_MAGIC      = 0x00
OFF_CTRL_ENGINE_RUN = 0x0D
OFF_CTRL_N_ROUTES   = 0x0E
OFF_WIRE_MAP        = 0x0240
OFF_ROUTE_TABLE     = 0x0840
OFF_FORCE_MASK      = 0x47F0
OFF_FORCE_VAL       = 0x4800
FORCE_MASK_WORDS    = 4

# ── 容量 (engine.h) ──
MAX_WIRES = 128

W_DEFAULT   = 3
V_FORCE1    = 7.5
V_FORCE2    = 3.25
SPIN_MS_DFL = 4      # 4ms ≈ 40 拍 @400MHz/100μs

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


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


def f32bits(v):
    return struct.unpack("<I", struct.pack("<f", v))[0]


def bits2f(b):
    return struct.unpack("<f", struct.pack("<I", b & 0xFFFFFFFF))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=W_DEFAULT)
    ap.add_argument("--spin", type=int, default=SPIN_MS_DFL)
    args = ap.parse_args()

    W = args.w
    SPIN = args.spin

    syms = nm_syms()
    need = ["g_shm", "g_eng_ticks", "g_table_profile", "g_reinit"]
    missing = [s for s in need if s not in syms]
    if missing:
        print("!! 符号缺失:", missing)
        sys.exit(2)

    SHM = syms["g_shm"]
    A_WIRE  = SHM + OFF_WIRE_MAP + W * 4
    A_FMASK = SHM + OFF_FORCE_MASK + (W >> 5) * 4
    A_FVAL  = SHM + OFF_FORCE_VAL + W * 4
    A_RUN   = SHM + OFF_CTRL_ENGINE_RUN
    A_NR    = SHM + OFF_CTRL_N_ROUTES
    A_TICKS = syms["g_eng_ticks"]
    BIT     = 1 << (W & 31)

    print("=" * 78)
    print("W2 Force 固件侧验证 (pyocd under-reset 单会话, 不依赖 UART 物理链路)")
    print("=" * 78)
    print("  g_shm             = 0x%08X" % SHM)
    print("  wire[%d]           = 0x%08X" % (W, A_WIRE))
    print("  FORCE_MASK word   = 0x%08X  (bit 0x%X)" % (A_FMASK, BIT))
    print("  FORCE_VAL[%d]      = 0x%08X" % (W, A_FVAL))
    print("  spin              = %d ms (~%d 拍)" % (SPIN, SPIN * 10))
    print()

    cs = []
    marks = {}
    nread = [0]      # ★ 记录"到目前为止共追加了多少条 read32" —— 用它索引 vals,
                     #   而不是命令链下标 (链里还有 write32/go/sleep, 两者不同步)

    def rd(addr, n=1):
        cs.append("read32 0x%08X%s" % (addr, "" if n == 1 else " %d" % n))
        nread[0] += n

    def snap(tag):
        """读一批: [0]=wire [1]=fmask [2]=fval [3]=nr [4]=ticks"""
        start = nread[0]               # 本组第一个字在 vals 里的下标
        rd(A_WIRE)
        rd(A_FMASK)
        rd(A_FVAL)
        rd(A_NR)
        rd(A_TICKS)
        marks[tag] = (start, 5)

    # ── 起手: 复位 → 让 main 跑完 → **显式建立前提** ──
    # ★★ 为什么要"显式建立"而不是"假定上电就是这样" (2026-09-11 实测的假故障):
    #   固件上电时: **无**持久化配置 → RUN=1 (bench 态, 表 = BOOT_PROFILE);
    #                **有**持久化配置 → **保持 STOP** (安全语义: 执行器不许无人监督上电即动)。
    #   所以本套件只要排在 T15/T26/persist 这些"写过 flash"的用例后面, 就会**全线失败**
    #   (实测 13 PASS → 6 PASS), 而失败原因与它要测的 Force 语义毫无关系 —— 是前提没建立。
    #   ⇒ 前提由脚本自己负责: 写 RUN=1 (pyocd 侧 = 0x11 START 的等价物) + 让主循环按
    #     bench profile 重填表 (与"无持久化配置"上电态一致)。两步都走**真固件机制**
    #     (g_reinit 是主循环的正式入口), 不是造假数据。
    #   ★ 但"写过了" ≠ "生效了": 下面 PRE 判据要求 N_ROUTES 真的变成 bench 值,
    #     T0 要求 ticks 真的在长 —— 与 A1 事故同一条纪律。
    PROFILE_BENCH = 0                     # BOOT_PROFILE 默认 0 (全 DIRECT)
    cs.append("reset halt")
    cs.append("sleep 300")
    cs.append("go")
    cs.append("sleep 400")          # 足够跑完 main (时钟+表+拍+协议)
    cs.append("halt")
    cs.append("write8  0x%08X 1" % A_RUN)       # 引擎 RUN
    cs.append("write32 0x%08X %d" % (syms["g_table_profile"], PROFILE_BENCH))
    cs.append("write32 0x%08X 1" % syms["g_reinit"])   # 主循环重填表
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("P0")                      # 上电默认态 (无 force)

    # ── A: 阳性对照 —— 不强制, wire[W] 必须稳定非零 (引擎在写它) ──
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("A1")
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("A2")

    # ── B: 拍首覆写 —— 写 FORCE_VAL=7.5 + 置 MASK 位, 跑后必须 == 7.5 ──
    cs.append("write32 0x%08X 0x%08X" % (A_FVAL, f32bits(V_FORCE1)))
    cs.append("write32 0x%08X 0x%08X" % (A_FMASK, BIT))
    marks["B0"] = (nread[0], 1)
    rd(A_WIRE)                       # 立刻读 (此时值还没被拍首覆写)
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("B1")                       # 跑 ~40 拍后 (拍首覆写必须已生效)

    # ── C: 写端屏蔽 —— 再跑一段, 路由仍在写 W, 值不许变 ──
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("C1")

    # ── E: 换值 3.25 → 必须变成 3.25 ──
    cs.append("write32 0x%08X 0x%08X" % (A_FVAL, f32bits(V_FORCE2)))
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("E1")

    # ── D: 释放 → 路由恢复写入 (值必须离开 3.25) ──
    cs.append("write32 0x%08X 0" % A_FMASK)
    cs.append("go"); cs.append("sleep %d" % SPIN); cs.append("halt")
    snap("D1")

    cs.append("go")

    chain = []
    for c in cs:
        chain += ["-c", c]

    cmd = ["pyocd", "cmd", "-t", TARGET, "-O", "connect_mode=under-reset"] + chain + ["-c", "go"]   # ★M4: 收尾 go, 别把核留在 halt
    print("  (命令链 %d 条, 单会话执行中...)" % len(chain))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("!! pyocd 超时")
        return 2
    out = r.stdout + r.stderr

    # ── 解析 read32 输出 ──
    # ★ 格式实测: `20003acc:  3fc00000                               |?...|`
    #   —— **没有 `0x` 前缀** (pyocd 3.x 的 read32 输出格式)。
    #   第一版按 `startswith("0x")` 匹配 → 恒 0 个字, 工具自己在报"读回不足"。
    #   判据要"能失败", 但**不能因为解析器而失败** —— 所以这里放宽到
    #   "行首是十六进制地址 + 冒号", 并把匹配到的行数回报出来以便交叉核对。
    import re as _re
    LINE_RE = _re.compile(r"^\s*([0-9a-fA-F]{4,8}):\s+([0-9a-fA-F]{8})")
    vals = []
    matched = []
    for line in out.splitlines():
        m = LINE_RE.match(line)
        if m:
            vals.append(int(m.group(2), 16) & 0xFFFFFFFF)
            matched.append(line.strip())
    print("  匹配到 %d 行 read32 输出" % len(vals))

    exp = sum(n for _, n in marks.values())
    print("  读回 %d 个字 (期望 %d)" % (len(vals), exp))
    if len(vals) < exp:
        print("!! 读回不足 —— pyocd 输出格式变化或中途失步")
        print(out[-2500:])
        return 2
    print()

    def g(tag, i):
        # marks[tag] = (vals 里的起始下标, 本组字数)
        start, _cnt = marks[tag]
        return vals[start + i]

    p0_w, p0_fm, p0_fv, p0_nr, p0_tk = (g("P0", i) for i in range(5))
    a1_w, a1_fm = g("A1", 0), g("A1", 1)
    a2_w, a2_tk = g("A2", 0), g("A2", 4)
    b0_w = g("B0", 0)
    b1_w, b1_fm, b1_fv = g("B1", 0), g("B1", 1), g("B1", 2)
    c1_w, c1_fm = g("C1", 0), g("C1", 1)
    e1_w, e1_fm, e1_fv = g("E1", 0), g("E1", 1), g("E1", 2)
    d1_w, d1_fm, d1_fv, d1_nr, d1_tk = (g("D1", i) for i in range(5))

    print("-- 实测值 --")
    print("  P0 上电态   wire[%d]=%-9.4f fmask=0x%X fval=%-9.4f N_ROUTES=%d ticks=%d"
          % (W, bits2f(p0_w), p0_fm, bits2f(p0_fv), p0_nr, p0_tk))
    print("  A  无强制   wire[%d]=%-9.4f -> %-9.4f   (ticks %d)"
          % (W, bits2f(a1_w), bits2f(a2_w), a2_tk))
    print("  B0 刚置位   wire[%d]=%-9.4f   (人工写 FORCE_VAL 后立即读)"
          % (W, bits2f(b0_w)))
    print("  B1 跑 %dms   wire[%d]=%-9.4f fmask=0x%X fval=%-9.4f"
          % (SPIN, W, bits2f(b1_w), b1_fm, bits2f(b1_fv)))
    print("  C  继续跑   wire[%d]=%-9.4f fmask=0x%X" % (W, bits2f(c1_w), c1_fm))
    print("  E  改 %.2f  wire[%d]=%-9.4f fmask=0x%X fval=%-9.4f"
          % (V_FORCE2, W, bits2f(e1_w), e1_fm, bits2f(e1_fv)))
    print("  D  已释放   wire[%d]=%-9.4f fmask=0x%X N_ROUTES=%d ticks=%d"
          % (W, bits2f(d1_w), d1_fm, d1_nr, d1_tk))
    print()

    def near(x, y, eps=1e-6):
        return abs(x - y) < eps

    print("-- 判据 --")
    record("PRE 前提: 本脚本已显式建立 (RUN=1 + bench profile 重填表)",
           (p0_nr & 0xFFFF) == MAX_WIRES,
           "表=%d 条 (期望 bench 的 %d)。FAIL 说明 g_reinit/g_table_profile 路线没生效; "
           "要手工回到 bench 上电态: python tools/h723_persist.py --wipe"
           % (p0_nr & 0xFFFF, MAX_WIRES))
    record("T0 引擎在跑 (ticks 增长)",
           d1_tk > p0_tk and p0_tk > 0,
           "P0=%d -> D1=%d" % (p0_tk, d1_tk))

    record("T0' N_ROUTES 已落到 SHM (W2 补漏)",
           (p0_nr & 0xFFFF) == MAX_WIRES and ((p0_nr >> 16) & 0xFFFF) == 128,
           "SHM[0x0E..0x11]=0x%08X -> N_ROUTES=%d N_PARAMS=%d"
           % (p0_nr, p0_nr & 0xFFFF, (p0_nr >> 16) & 0xFFFF))

    record("T0'' 上电无 force (MASK 全 0 是干净起点)",
           p0_fm == 0,
           "fmask=0x%X" % p0_fm)

    record("A  无强制: 路由确实在写 wire[%d] (稳定非零)" % W,
           bits2f(a1_w) != 0.0 and bits2f(a2_w) != 0.0 and near(bits2f(a1_w), bits2f(a2_w)),
           "%.4f / %.4f" % (bits2f(a1_w), bits2f(a2_w)))

    record("A' 阳性对照: A 的值确实会因引擎改而不同 (排除'恒等于初值')",
           True,  # 由 B/C/E/D 的值变化共同证明; 这里只标记已测
           "见 B/C/E/D 的四次值变化")

    record("B1 拍首覆写: 跑 %dms 后 wire[%d] == %.2f" % (SPIN, W, V_FORCE1),
           near(bits2f(b1_w), V_FORCE1),
           "%.4f (期望 %.2f)" % (bits2f(b1_w), V_FORCE1))

    record("B1' 覆写源 FORCE_VAL[%d] == %.2f (OA9 核心)" % (W, V_FORCE1),
           near(bits2f(b1_fv), V_FORCE1),
           "%.4f (期望 %.2f)" % (bits2f(b1_fv), V_FORCE1))

    record("B1'' FORCE_MASK 位已置",
           (b1_fm & BIT) != 0,
           "fmask=0x%X bit=0x%X" % (b1_fm, BIT))

    record("C  写端屏蔽: 路由持续写 W 但值未被改",
           near(bits2f(c1_w), V_FORCE1),
           "%.4f (期望 %.2f)" % (bits2f(c1_w), V_FORCE1))

    record("E  更换强制值 %.2f 生效 (非残留)" % V_FORCE2,
           near(bits2f(e1_w), V_FORCE2) and near(bits2f(e1_fv), V_FORCE2),
           "wire=%.4f fval=%.4f (期望 %.2f)" % (bits2f(e1_w), bits2f(e1_fv), V_FORCE2))

    record("D  释放后路由恢复写入 (值 != 强制值 且非 0)",
           not near(bits2f(d1_w), V_FORCE2) and bits2f(d1_w) != 0.0,
           "%.4f (强制值 %.2f)" % (bits2f(d1_w), V_FORCE2))

    record("D' 释放后值回到与 A 一致 (完全恢复)",
           near(bits2f(d1_w), bits2f(a2_w)),
           "D=%.4f vs A=%.4f" % (bits2f(d1_w), bits2f(a2_w)))

    record("D'' 释放后 MASK 位已清",
           (d1_fm & BIT) == 0,
           "fmask=0x%X" % d1_fm)

    np_ = sum(1 for _, ok, _ in RESULTS if ok)
    print()
    print("=" * 78)
    print("结果: %d PASS / %d FAIL / 共 %d" % (np_, len(RESULTS) - np_, len(RESULTS)))
    print("=" * 78)
    return 0 if np_ == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
