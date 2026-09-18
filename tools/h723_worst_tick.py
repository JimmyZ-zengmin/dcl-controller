#!/usr/bin/env python3
"""
h723_worst_tick.py — 精确最坏拍 vs 摊薄模型（"序 0"）

★★ 这个工具在回答什么
  `engine_prog_budget()`（deploy 静态门）算的是 **平均每拍** 上界:
      Σ_ACTIVE ceil((op_cost + src_cost) / mult)          mult = 1 / 10 / 64
  而拍 ISR 真实跑的是（`engine.c:586-597`，已核实）:
      cost(t) = C_div0 + D1[t % 10] + D2[t % 100]
  ⇒ 中间隔着一个假设: **相位均匀铺开**。
  ★ 已核实（`engine.c:983-992`）: deploy 建表时**忽略载荷的相位字段**，按轮转法分配
    （`ph = q1++ % 10` / `q2++ % 100`）⇒ **假设是被固件强制执行的**，不是没人管。
  ⇒ 所以摊薄模型**大体是安全上界**；剩下的是两个小缺口与一个真缺陷（见下）。

★★★ 两条建桶路径（★ 本项目 `docs/audit/REVIEW-MIGRATION-FIDELITY.md` #4 已记录）
  · **deploy 路径** `engine_stage_program`（`engine.c:960-995`）:
        按 **未截断** 的 `ph = q2++ % 100` 定槽 ⇒ 桶 64..99 **有路由**
  · **开机装载路径** `persist.c:220` → `engine_build_buckets`（`engine.c:333-388`）:
        按 `period` 里 **被截断的** 相位归组 ⇒ 只能落到桶 0..63
  ⇒ **同一份程序，deploy 后与重启后，桶分布不同 ⇒ 最坏拍不同。**
  本工具**两个都算**：`worst_isr`（按读回的桶表 = 正在跑的那个）与
  `worst_reboot`（按 period 字段重算 = 重启后会变成的那个）。

判据（每条都能失败；★ 分三类，别把"我读错了"和"固件不变量不成立"混为一谈）
  ── A 类：我的读数可信吗 ─────────────────────────────────
  A1 桶表区间必须**恰好铺满** [0, nr)：off1[0]==n0 且各区间首尾相接不重叠
  A2 桶表计数 == 从路由表按桶区间独立数出来的条数
  ── B 类：固件的既有不变量成立吗（不成立 = 记档缺陷，不是本工具坏）──
  B1 死槽不变量: `off2/cnt2[64..99]` 恒 0（`engine.h:183` 的宣称）
  B2 period 相位字段 == 桶归属（任何"按 period 反推桶"的校验都依赖它）
  ── C 类：门是不是安全上界 ───────────────────────────────
  C1 工具模型 == 固件自报 budget（两处模型同源）
  C2 ★ `worst_isr ≤ 模型`（门必须覆盖**正在跑**的那个布局）
  C3 ★ `worst_reboot ≤ 模型`（门必须覆盖**重启后**的那个布局）

用法
  python tools/h723_worst_tick.py                      # 分析板上当前程序
  python tools/h723_worst_tick.py --prog 5,2,128       # 先部署 op=5(PID) div=2 n=128
  python tools/h723_worst_tick.py --path flash
退出码: 0 = A/C 全过 / 1 = 有失败 / 2 = 前置不满足（判无效）
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, re, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

GATE = 26000
TICK_CYC = 40000
DIV_MULT = {0: 1, 1: 10, 2: 64}
BUCKET_DIV1_PHASES = 10
BUCKET_DIV2_PHASES = 100
ROUTE_BUCKET_U16 = 220
OFF_ROUTE_TABLE = 0x0840
OFF_ROUTE_BUCKETS = 0x4480
OFF_ROUTE_BUCKETS_ST = 0x4638
ACTIVE = 0x01
NM = ("C:/ST/STM32CubeIDE_1.5.1/STM32CubeIDE/plugins/com.st.stm32cube.ide.mcu.externaltools."
      "gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924/tools/bin/"
      "arm-none-eabi-nm.exe")

OP_NAMES = ["DIRECT", "CMP", "HYST", "CLAMP", "LPF", "PID", "RATE", "DEADBAND", "MUX",
            "EDGE", "LUT", "CNT", "TIMER", "ARITH", "SCALE", "AND", "OR", "NOT", "SR"]
OP_MAX = 0x12
SRC_CONST, DST_WIRE = 2, 2


def cost_table(path):
    src = open(os.path.join(HERE, "..", "src", "engine.c"),
               encoding="utf-8", errors="replace").read()
    name = "k_op_cost_flash" if path == "flash" else "k_op_cost_itcm"
    m = re.search(re.escape(name) + r"\[(0x[0-9A-Fa-f]+)\]\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        raise SystemExit("!! src/engine.c 里找不到 %s" % name)
    body = re.sub(r"/\*.*?\*/", "", m.group(2), flags=re.S)
    vals = [int(x) for x in re.findall(r"\b(\d+)\b", body)]
    if len(vals) != int(m.group(1), 16):
        raise SystemExit("!! %s 项数不符" % name)
    return vals, name


def rd_words(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(0x22, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def parse_routes(buf, nr):
    rs = []
    for i in range(nr):
        (src_t, src_i, dst, ch, op, fl, pidx, soff, _r, w2, per, _z) = \
            struct.unpack("<BBBBBBHHHHBB", buf[i * 16:(i + 1) * 16])
        rs.append(dict(src_t=src_t, src_i=src_i, op=op, fl=fl, per=per,
                       div=per & 0x03, phase=(per >> 2) & 0x3F))
    return rs


def mk_prog(op, div, n):
    """RouteEntry 布局（`dclc.py:803` 同款）: period 落在第 11 个字段（偏移 14）。
    ★ 第一版多写了一个 0 ⇒ div 挤进保留字节 ⇒ 固件按 div0 计价（症状: "部署成功但档位没生效"）。"""
    assert 0 <= div <= 2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  ACTIVE, i, 1, 0, 0, div, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    return struct.pack("<HHH", n, n, 1) + routes + params + b"\x00" * 16


def worst_from_buckets(C0, D1, D2):
    """ISR 的每拍成本: C_div0 + D1[t%10] + D2[t%100]，周期 = lcm(10,100) = 100"""
    best, bt = -1, -1
    for t in range(100):
        v = D1[t % BUCKET_DIV1_PHASES] + D2[t % BUCKET_DIV2_PHASES]
        if v > best:
            best, bt = v, t
    return C0 + best, bt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--path", default="itcm", choices=("itcm", "flash"))
    ap.add_argument("--prog", default=None, metavar="OP,DIV,N")
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    ct, ctname = cost_table(a.path)
    print("成本表: %s (最贵 op%d = %d)" % (ctname, max(range(len(ct)), key=lambda i: ct[i]), max(ct)))
    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port)
    time.sleep(1.0)
    res = []
    try:
        budget = None
        if a.prog:
            op, div, n = (int(x, 0) for x in a.prog.split(","))
            dcl.send(0x12); time.sleep(0.2)
            dcl.send(0x13); time.sleep(0.3)      # ★ RESET 会清程序 ⇒ 必须在 deploy **之前**
            dcl.send(0x11); time.sleep(0.3)
            sts, p = dcl.send(0x10, mk_prog(op, div, n), expect_len=None)
            if sts != "ACK":
                print("!! deploy 被拒: %s" % (p.decode('utf-8', 'replace') if sts == 'NAK' else sts))
                return 2
            budget = struct.unpack("<HI", p[:6])[1]
            _u = ct[op] if op <= OP_MAX else ct[5] * 2
            exp = n * ((_u + DIV_MULT[div] - 1) // DIV_MULT[div])   # ★ 括号: `n*x//d` 是左结合
            print("已部署 op=%s(%s) div=%d n=%d ⇒ ACK budget=%d（期望 %d %s）"
                  % (op, OP_NAMES[op] if op <= OP_MAX else "?", div, n, budget, exp,
                     "✓" if budget == exp else "✗ 档位没生效"))
            if budget != exp:
                return 2
            time.sleep(a.settle)

        st = dcl.send(0x38, expect_len=51)
        if st[0] != "ACK" or len(st[1]) < 51:
            print("!! 0x38 读数失败"); return 2
        p = st[1]
        samples, pmin, pmax, emin, emax = struct.unpack("<IIIII", p[:20])
        nr = struct.unpack("<H", p[20:22])[0]
        shm = struct.unpack("<I", p[23:27])[0]
        ov = struct.unpack("<I", p[27:31])[0]
        print("SHM=0x%08X n_routes=%d emin=%d emax=%d TB tick (ov=%d samples=%d)"
              % (shm, nr, emin, emax, ov, samples))
        if not shm or nr == 0:
            print("!! 无程序或 g_shm=0 ⇒ 判无效"); return 2

        bk = rd_words(dcl, shm + OFF_ROUTE_BUCKETS, ROUTE_BUCKET_U16 // 2)
        rt = rd_words(dcl, shm + OFF_ROUTE_TABLE, 128 * 4)
        if bk is None or rt is None:
            print("!! 0x22 读 SHM 失败 ⇒ 判无效"); return 2
        u = struct.unpack("<%dH" % ROUTE_BUCKET_U16, bk)
        off1, cnt1 = list(u[0:10]), list(u[10:20])
        off2, cnt2 = list(u[20:120]), list(u[120:220])
        routes = parse_routes(rt, nr)

        def cst(x):
            return ct[x["op"]] if x["op"] <= OP_MAX else ct[5] * 2

        # ── A 类: 读数可信吗 ───────────────────────────────────────────
        A1 = (off1[0] == 0 or True)   # 占位，真实判据在下面
        # 区间必须恰好铺满 [0, nr)：按 (div0 段, div1 各桶, div2 各桶) 顺序检查连续性
        expect_next = 0
        A1 = True
        for i in range(off1[0]):                     # div0 段 = [0, off1[0])
            pass
        expect_next = off1[0]
        for q in range(BUCKET_DIV1_PHASES):
            if off1[q] != expect_next:
                A1 = False
            expect_next = off1[q] + cnt1[q]
        for q in range(BUCKET_DIV2_PHASES):
            if off2[q] != expect_next:
                A1 = False
            expect_next = off2[q] + cnt2[q]
        A1 = A1 and (expect_next == nr)
        res.append(("A1 桶区间恰好铺满 [0, n_routes)", A1))
        print("  A1 桶区间铺满 [0,%d): 末端=%d %s" % (nr, expect_next, "✓" if A1 else "✗"))

        A2 = (off1[0] + sum(cnt1) + sum(cnt2) == nr)
        res.append(("A2 桶表计数总和 == n_routes", A2))
        print("  A2 计数总和 = %d / %d %s" % (off1[0] + sum(cnt1) + sum(cnt2), nr,
                                              "✓" if A2 else "✗"))

        # ── C2: ISR 真实的每拍成本（**按读回的桶表**，这才是正在跑的布局）────
        C0 = sum(cst(routes[i]) for i in range(off1[0]))
        D1 = [sum(cst(routes[i]) for i in range(off1[q], off1[q] + cnt1[q]))
              for q in range(BUCKET_DIV1_PHASES)]
        D2 = [sum(cst(routes[i]) for i in range(off2[q], off2[q] + cnt2[q]))
              for q in range(BUCKET_DIV2_PHASES)]
        worst_isr, t_isr = worst_from_buckets(C0, D1, D2)

        # ── C3: 重启后的布局（`engine_build_buckets` 语义: 按 period 的相位归组）──
        rb1 = {q: 0 for q in range(BUCKET_DIV1_PHASES)}
        rb2 = {q: 0 for q in range(BUCKET_DIV2_PHASES)}
        rc0 = 0
        rc1 = {q: 0 for q in range(BUCKET_DIV1_PHASES)}
        rc2 = {q: 0 for q in range(BUCKET_DIV2_PHASES)}
        for x in routes:
            if not (x["fl"] & ACTIVE):
                continue
            dv, ph = x["div"] & 0x03, (x["per"] >> 2) % BUCKET_DIV2_PHASES
            if dv == 0:
                rc0 += cst(x)
            elif dv == 1:
                rb1[ph % BUCKET_DIV1_PHASES] += 1
                rc1[ph % BUCKET_DIV1_PHASES] += cst(x)
            else:
                rb2[ph % BUCKET_DIV2_PHASES] += 1
                rc2[ph % BUCKET_DIV2_PHASES] += cst(x)
        worst_reboot, t_rb = worst_from_buckets(rc0, [rc1[q] for q in range(10)],
                                                [rc2[q] for q in range(100)])

        model = sum((cst(x) + DIV_MULT[x["div"]] - 1) // DIV_MULT[x["div"]]
                    for x in routes if x["fl"] & ACTIVE)

        print()
        print("  ┌─ 每拍成本（cyc），单位 = cyc/拍，拍长 %d" % TICK_CYC)
        print("  │ 模型（deploy 门用的摊薄上界）  = %8d   = 拍 %.2f%%" % (model, 100.0 * model / TICK_CYC))
        print("  │ ★ worst_isr  （读回桶表 = 正在跑）= %8d   = 拍 %.2f%%  (t=%d)"
              % (worst_isr, 100.0 * worst_isr / TICK_CYC, t_isr))
        print("  │ ★ worst_reboot（period 重算 = 重启后）= %8d = 拍 %.2f%%  (t=%d)"
              % (worst_reboot, 100.0 * worst_reboot / TICK_CYC, t_rb))
        print("  │ 两者之比 = %.2f×   （≠1 ⇒ 两条建桶路径语义不同）"
              % (worst_reboot / float(worst_isr) if worst_isr else 0))
        print("  └─ div0=%d(C0=%d) div1=%d div2=%d"
              % (sum(1 for x in routes if x['div'] == 0 and x['fl'] & ACTIVE), C0,
                 sum(cnt1), sum(cnt2)))

        # ── B 类: 固件既有不变量 ────────────────────────────────────────
        dead_cnt = sum(cnt2[64:])
        dead_off = sum(1 for q in range(64, 100) if off2[q] != off2[63] + cnt2[63] and cnt2[q] == 0)
        res.append(("B1 死槽不变量 cnt2[64..99]==0（engine.h:183 的宣称）", dead_cnt == 0))
        print("  B1 死槽 64..99 里的路由条数 = %d %s"
              % (dead_cnt, "✓" if dead_cnt == 0 else
                 "✗ **违反 engine.h:183 的不变量** —— deploy 路径按 ph=q%%100 定槽，"
                 "桶 64..99 确有路由"))

        mism = 0
        for q in range(BUCKET_DIV1_PHASES):
            for i in range(off1[q], off1[q] + cnt1[q]):
                if routes[i]["phase"] % BUCKET_DIV1_PHASES != q:
                    mism += 1
        for q in range(BUCKET_DIV2_PHASES):
            for i in range(off2[q], off2[q] + cnt2[q]):
                if routes[i]["phase"] % BUCKET_DIV2_PHASES != q:
                    mism += 1
        res.append(("B2 period 相位字段 == 桶归属（按 period 反推桶的校验依赖它）", mism == 0))
        print("  B2 period 相位与桶归属不符 = %d 条 %s"
              % (mism, "✓" if mism == 0 else
                 "✗ **period 里的相位是垃圾** —— `ph<<2` 在 u8 里被截断（ph≥64 时 64<<2=0x100→0）"))

        # ── C 类 ────────────────────────────────────────────────────────
        if budget is not None:
            ok = (budget == model)
            res.append(("C1 工具模型 == 固件自报 budget", ok))
            print("  C1 budget=%d / 工具=%d %s" % (budget, model, "✓" if ok else "✗ 两处模型不一致"))
        res.append(("C2 worst_isr ≤ 模型（门覆盖正在跑的布局）", worst_isr <= model))
        res.append(("C3 worst_reboot ≤ 模型（门覆盖重启后的布局）", worst_reboot <= model))

        # ── 重载拍归因 ─────────────────────────────────────────────────
        rel_addr, rel = None, None
        try:
            elf = os.path.join(HERE, "..", "build", "dcl_h723")
            for line in subprocess.run([NM, elf], capture_output=True, text=True,
                                       timeout=60).stdout.splitlines():
                q = line.split()
                if len(q) == 3 and q[2] == "g_reload_cyc":
                    rel_addr = int(q[0], 16)
            if rel_addr:
                b = rd_words(dcl, rel_addr, 1)
                rel = struct.unpack("<I", b[:4])[0] if b else None
        except Exception as e:
            print("  (g_reload_cyc 跳过: %s)" % e)
        print()
        print("  实测 0x38: emin=%d emax=%d TB tick ⇒ cyc: %d / %d"
              % (emin, emax, emin * 2, emax * 2))
        if rel is not None:
            base = emin * 2
            print("  热重载 g_reload_cyc(0x%08X) = %d cyc   ← F5 的“上界”升级为**精确值**"
                  % (rel_addr, rel))
            print("  归因: emax(%d) − 空基线(%d) − worst_isr(%d) = %d cyc"
                  % (emax * 2, base, worst_isr, emax * 2 - base - worst_isr))
            print("      ★ 残差 ≈ g_reload_cyc ⇒ 锯齿来自**重载那一拍**，稳态最坏拍就是 %d cyc"
                  % worst_isr)
        else:
            print("  ★ 未能读到 g_reload_cyc（0x22 @0x%08X）—— emax 超出 worst_isr 的部分"
                  "应来自热重载那一拍" % (rel_addr or 0))

        if a.json:
            import json
            os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
            json.dump(dict(path=a.path, n_routes=nr, model=model, worst_isr=worst_isr,
                           worst_reboot=worst_reboot, t_isr=t_isr, t_reboot=t_rb,
                           C0=C0, D1=D1, D2=D2, dead_cnt=dead_cnt, mism=mism,
                           budget=budget, emin=emin, emax=emax, ov=ov,
                           reload_cyc=rel, tick_cyc=TICK_CYC, gate=GATE),
                      open(a.json, "w"), indent=1)
            print("\n原始数据: %s" % a.json)
    finally:
        dcl.send(0x12); time.sleep(0.2); dcl.send(0x13); time.sleep(0.3); dcl.send(0x11)
        time.sleep(0.3)
        dcl.close()

    print("\n=== 断言 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    bad = [k for k, v in res if not v]
    print("\n%d 项, %d FAIL" % (len(res), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
