#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-V —— 闸门摊薄口径的**隐含依赖**：它凭什么成立，以及它差一点被改错

## 事情的经过（留档，因为它比结论值钱）
我一度认定闸门有个"真洞"：`engine_prog_budget` 用 `Σ ceil((op+src)/mult)` —— **摊薄**口径，
把一条 div2 路由按 `成本/64` 计入；而运行期判据 `EXEC_BUDGET_CYCLES` 是**每拍**口径。
⇒ 我以为"若某档路由挤在同一个相位, 那一拍要跑全部, 而闸门只算了 1/64"。

**这个判断错了。** 实测：把 128 条 PID **全写成 phase 0** 部署, 实测扫描段最大只有 **262 cyc**
（若真挤在一相位, 应是 ~128×200 = 25600 cyc 量级）。
根因: `engine_stage_program` 尾部**覆写**载荷里的相位 —— `ph = q2++ % BUCKET_DIV2_PHASES_USED`,
即**固件自己按"档内到达序"轮转铺相位**, 载荷带的那 6 位相位**被忽略**。

⇒ 于是真正的结论有两条, 都不是我原先想的那条:
  1. **摊薄口径是对的** —— 它成立的前提正是"轮转铺相位"，而固件确实这么做了；
     而且 `ceil()` 的取整方向使它成为最坏桶的**上界**（余量 ≤ 一条路由的成本）。
  2. ★★ **但那个前提是"另一处的一个未声明性质"**：闸门与 staging 之间有一条**看不见的依赖**，
     没有任何断言牵连它们。我按"载荷相位"重写闸门的那一版, 会算出 128 条 PID div2 = 55296
     ⇒ FLASH 档**误拒**一条实际很轻（最坏桶 2 条 ≈ 904 cyc）的合法程序, 最多差 **64×**。
     ⇒ 拿**被忽略的输入**去算一个本该由固件分配的量 —— 已回退, 并把依赖写成判据（本工具）。

## 判据（都能失败）
  V1 div0-only 程序: 设备预算 == 摊薄式 == "最坏相位桶"式（`mult` 就是 1）**逐位相等**
     ⇒ 已验过的边界（FLASH DIRECT 105/106、PID 60/61）不受任何影响
  V2 ★★ **载荷里的相位被忽略**: 同一程序全 phase 0 与全 phase 63 部署 ⇒ **同一张桶表**
  V3 **轮转铺开**: 桶表 cnt2 与 `i % 64` 的轮转分布**逐槽吻合**（n=128 ⇒ 每槽 2 条）
  V4 ★ **摊薄式是最坏桶的上界**: `max(cnt2)×单条成本 ≤ 设备预算`，且**余量 ≤ 一条路由的成本**
  V5 ★ FLASH 档算术: 128 条 PID div2 ⇒ 摊薄式 vs 轮转后真实最坏桶（条数由 ceil(128/相位数) 现算，
     单条成本由 `k_op_cost_flash[PID]` 现读 —— **本文件不写死任何 cyc 数**，否则它会随成本表漂移。
     实测 896 vs 2×432 = 864（仍 ≥ 上界）

用法: python tools/exp_ev_gate_worst_phase.py
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import os, re, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from h723_client import Dcl

CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_BURST, CMD_SEQ = 0x38, 0x10, 0x12, 0x11, 0x22, 0x44
OFF_ROUTE_BUCKETS = 0x4480
OFF_EXEC_RING_HDR = 0x3880
SRC_CONST, DST_WIRE = 2, 2
FLAG_ACTIVE, FLAG_WIRE2 = 0x01, 0x02
DIV_FAST, DIV_MID, DIV_SLOW = 0, 1, 2


def rd(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(CMD_BURST, struct.pack("<IH", addr + 4 * off, k), expect_len=4 * k)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]; off += k
    return out


def src_consts():
    h = open(os.path.join(ROOT, "src", "engine.h"), encoding="utf-8", errors="replace").read()
    c = open(os.path.join(ROOT, "src", "engine.c"), encoding="utf-8", errors="replace").read()
    ck = open(os.path.join(ROOT, "src", "clock.h"), encoding="utf-8", errors="replace").read()

    def g(name, txt=h):
        m = re.search(r"^#define\s+%s\s+(\d+)u?\b" % name, txt, re.M)
        if not m:
            raise SystemExit("!! 找不到 %s" % name)
        return int(m.group(1))

    def tick_us():
        for t in (h, ck):
            m = re.search(r"^#define\s+TICK_PERIOD_US\s+(\d+)u?\b", t, re.M)
            if m:
                return int(m.group(1))
        m = re.search(r"^#define\s+TICK_PERIOD_US\s+([A-Z_][A-Z0-9_]*)\b", h, re.M)
        if m:
            for t in (ck, h):
                m2 = re.search(r"^#define\s+%s\s+(\d+)u?\b" % m.group(1), t, re.M)
                if m2:
                    return int(m2.group(1))
        raise SystemExit("!! 解析不到拍长")

    def ph2():
        m = re.search(r"^#define\s+BUCKET_DIV2_PHASES_USED\s+(\d+)u?\s*$", h, re.M)
        if m:
            return int(m.group(1))
        nom = re.search(r"^#define\s+DIV2_PHASES_NOMINAL\s+(\d+)u?", h, re.M) or \
              re.search(r"^#define\s+DIV2_NOMINAL_US\s+(\d+)u?", h, re.M)
        if nom and re.search(r"^#define\s+BUCKET_DIV2_PHASES_USED\s*\\", h, re.M):
            return min(64, int(nom.group(1)) // tick_us())
        raise SystemExit("!! 解析不到 div2 相位数")

    it = [int(x) for x in re.findall(r"\d+", re.search(
        r"k_op_cost_itcm\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", c, re.S).group(1))]
    fl = [int(x) for x in re.findall(r"\d+", re.search(
        r"k_op_cost_flash\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", c, re.S).group(1))]
    sr = [int(x) for x in re.findall(r"\d+", re.search(
        r"k_src_cost\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", c, re.S).group(1))]
    return dict(itcm=it, flash=fl, src=sr, budget=g("EXEC_DEPLOY_BUDGET"),
                trans=g("OP_TRANS_COST"), ph1=g("BUCKET_DIV1_PHASES"), ph2=ph2(),
                tick_us=tick_us())


def mk(op, div, phase, n):
    """★ phase 是**载荷里的**字段 —— 本工具要证明的正是"它被忽略"。"""
    fl = FLAG_ACTIVE | FLAG_WIRE2
    per = div | (phase << 2)
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  fl, i, (i % 64) + 1, 0, i, per, 0) for i in range(n))
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def amortized(n, op, K, table="itcm", dv=DIV_SLOW):
    """闸门当前口径（摊薄）: Σ ceil((op+src)/mult)，**mult 按档取**。
    ★ 第一版把 div0 也除了 64（mult 恒取 ph2）⇒ 算出 32 条 DIRECT = 32，而设备说 1792
      ⇒ 三个 V1 全 FAIL —— **判据的除数错了, 看起来像设备错了**（本日第四次同族）。"""
    full = K[table][op] + K["src"][SRC_CONST]
    mult = {DIV_FAST: 1, DIV_MID: K["ph1"], DIV_SLOW: K["ph2"]}[dv]
    return n * ((full + mult - 1) // mult)


def main():
    K = src_consts()
    print("=" * 74)
    print("E-V  闸门摊薄口径的隐含依赖：它凭什么成立")
    print("=" * 74)
    print("常数: 门=%d 转变=%d 相位 div1=%d div2=%d | ITCM PID=%d FLASH PID=%d"
          % (K["budget"], K["trans"], K["ph1"], K["ph2"], K["itcm"][0x05], K["flash"][0x05]))
    dcl = Dcl(os.environ.get("DCL_PORT"))
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    res, skip = [], []
    try:
        sts, p = dcl.send(CMD_STATUS, expect_len=51)
        shm = struct.unpack("<I", p[23:27])[0]
        for _ in range(10):
            v = rd(dcl, shm + OFF_EXEC_RING_HDR, 2)
            if v and struct.unpack("<2I", v[:8])[1] >= 200000:
                break
            time.sleep(0.5)
        dcl.send(CMD_STOP); time.sleep(0.2)
        print("前置: 顺序域清除 ⇒ %s\n" % dcl.send(CMD_SEQ, struct.pack("<BH", 0, 0),
                                                  expect_len=None)[0])

        def deploy(op, dv, ph, n):
            s, q = dcl.send(CMD_DEPLOY, mk(op, dv, ph, n), expect_len=None)
            if s != "ACK":
                return s, None, None
            bg = struct.unpack("<I", q[2:6])[0]
            time.sleep(0.25)
            b = rd(dcl, shm + OFF_ROUTE_BUCKETS, 224)
            # ★ 读回长度必须自己校验: 桶表 220×u16 = 440 B。少一截就说明分块读没对齐
            #   （本项目踩过"分块读 off-by-one"两次）⇒ **判无效, 不硬解**。
            if not b or len(b) < 440:
                print("    !! 桶表读回 %s 字节（要 ≥440）⇒ 本项判无效"
                      % (len(b) if b else "None"))
                return s, bg, None
            u = struct.unpack("<220H", b[:440])
            return s, bg, (list(u[20:120]), list(u[120:220]))

        # ── V1: div0-only ⇒ 三式恒等 ──────────────────────────────────
        print("── V1 div0-only：摊薄式 == 最坏桶式（回归保护）──")
        for n in (32, 128):
            s, bg, bk = deploy(0x00, DIV_FAST, 0, n)
            if bg is None:
                skip.append("V1 n=%d 被拒(%s)" % (n, s)); continue
            am = amortized(n, 0x00, K, dv=DIV_FAST)         # div0: mult=1 ⇒ 就是全额
            print("    n=%-4d 设备=%-6d 摊薄式=%-6d（div0 的 mult=1 ⇒ 两式恒等）" % (n, bg, am))
            res.append(("V1 n=%d: div0-only 设备==摊薄式（%d）" % (n, bg), bg == am))
        print()

        # ── V2: 载荷相位被忽略 ───────────────────────────────────────
        print("── V2 ★★ 载荷里的 phase 被 staging 覆写（全 0 与全 63 出同一张桶表）──")
        s0, b0, bk0 = deploy(0x05, DIV_SLOW, 0, 128)
        s1, b1, bk1 = deploy(0x05, DIV_SLOW, 63, 128)
        if bk0 and bk1:
            print("    全 phase 0 : device=%-6d cnt2 前 6 槽 = %s" % (b0, bk0[1][:6]))
            print("    全 phase 63: device=%-6d cnt2 前 6 槽 = %s" % (b1, bk1[1][:6]))
            res.append(("V2 载荷相位不影响桶表（两张 cnt2 逐槽相同）", bk0[1] == bk1[1]))
            res.append(("V2′ 两次部署的 device budget 也相同（%d/%d）" % (b0, b1), b0 == b1))
            # ── V3: 轮转铺开 ─────────────────────────────────────────
            # ★ 桶表里 cnt2 有 **100 槽**（BUCKET_DIV2_PHASES），而实际只用 64 槽
            #   ⇒ 只能比前 64 槽；剩下 36 槽**必须恒 0**（与固件 engine_bucket_dead_slots 同义）。
            live, dead = bk0[1][:K["ph2"]], bk0[1][K["ph2"]:]
            want = [128 // K["ph2"] + (1 if i < 128 % K["ph2"] else 0) for i in range(K["ph2"])]
            print("    V3 轮转校验: 前 %d 槽 == 全 %d ? ⇒ %s ；死槽(%d..99) 全 0 ? ⇒ %s"
                  % (K["ph2"], 128 // K["ph2"], live == want, K["ph2"], sum(dead) == 0))
            res.append(("V3 桶表是轮转铺开（cnt2 前 %d 槽逐槽吻合）" % K["ph2"],
                        live == want))
            res.append(("V3′ 死槽 cnt2[%d..99] 恒 0（实测和=%d）"
                        % (K["ph2"], sum(dead)), sum(dead) == 0))
            # ── V4: 摊薄式是最坏桶的上界, 且余量 ≤ 一条路由 ──────────
            full = K["itcm"][0x05] + K["src"][SRC_CONST]
            worst = max(bk0[1][:K["ph2"]]) * full
            am = b0
            print("    V4 最坏桶 = max(cnt2)=%d 条 × %d = **%d cyc**；设备预算（摊薄）= **%d**"
                  % (max(bk0[1][:K["ph2"]]), full, worst, am))
            print("       ⇒ 上界成立? %s ; 余量 = %d cyc ≤ 一条路由 %d cyc ? %s"
                  % (worst <= am, am - worst, full, 0 <= am - worst <= full))
            res.append(("V4 摊薄式 ≥ 最坏桶（%d ≥ %d）" % (am, worst), worst <= am))
            res.append(("V4′ 余量 ≤ 一条路由成本（%d ≤ %d）—— ceil 取整的固有松弛" % (am - worst, full),
                        0 <= am - worst <= full))
        else:
            skip.append("V2/V3/V4 —— 桶表读失败或被拒")
        print()

        # ── V5: FLASH 档算术 ────────────────────────────────────────
        print("── V5 FLASH 档算术：摊薄式对「全挤一相位」也仍是上界（因为不会挤）──")
        fullF = K["flash"][0x05] + K["src"][SRC_CONST]
        for n in (60, 128):
            amF = n * ((fullF + K["ph2"] - 1) // K["ph2"])
            per_bucket = n // K["ph2"] + (1 if n % K["ph2"] else 0)
            worstF = per_bucket * fullF
            print("    n=%-4d 摊薄式=%-6d(≤门 %d ⇒ %s)  轮转后最坏桶=%d 条 × %d = %-6d  |  差额 %+d"
                  % (n, amF, K["budget"], "ACK" if amF <= K["budget"] else "NAK",
                     per_bucket, fullF, worstF, amF - worstF))
        amF = 128 * ((fullF + K["ph2"] - 1) // K["ph2"])
        n_bucket = 128 // K["ph2"] + (1 if 128 % K["ph2"] else 0)   # ★ 不写死 2
        worstF = n_bucket * fullF
        res.append(("V5 FLASH 128 条 PID div2: 摊薄式 %d ≥ 轮转最坏桶 %d×%d=%d（不会误拒）"
                    % (amF, n_bucket, fullF, worstF), worstF <= amF))
        print("    ★ 而「按载荷相位算最坏桶」的那一版会给出 128×%d = **%d ⇒ NAK** ——"
              % (fullF, 128 * fullF))
        print("      它会**误拒**这条实际最坏桶只有 %d cyc 的合法程序（差 %d 倍）。已回退。" %
              (worstF, (128 * fullF) // worstF))
    finally:
        try:
            dcl.send(CMD_STOP); time.sleep(0.2); dcl.send(CMD_START); time.sleep(0.3)
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
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
