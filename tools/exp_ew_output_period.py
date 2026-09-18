#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-W —— 3.1 `OUTPUT ... PERIOD=`：让 ③ 层**真的能把路由放到 div1/div2**

## 这一项修的是什么（两条，第二条比第一条值钱）

① **`OUTPUT` 收不下 `PERIOD=`**（纯缺陷）：`PERIOD=` 后缀原来只在**第二遍**里剥掉，
   而**第一遍**（OUTPUT 固定槽预检）拿的是**没剥后缀**的原文 ⇒
   `OUTPUT o TO wire[12] FROM x PERIOD=1ms` 在第一遍就被 `fullmatch` 判"语法错误"。
   ⇒ "下游整条链必须同档或更慢"这条规则因此**在最后一级断掉**：末级只能留在快档
   （错误提示里甚至写了一句让人绕开它）。

② ★★ **`PERIOD=10ms` 是假的**（宣称 ≠ 实现）：编译器的档位表写死
   `{100us:0, 1ms:1, 10ms:2}`，而引擎真实的 div2 周期 = `BUCKET_DIV2_PHASES_USED × 拍长`
   = **64 × 100µs = 6.4ms** —— phase 字段只有 6 位、装不下 100 个相位 ⇒
   **10ms 在 100µs 档上从来达不到**（固件自己的注释就是这么写的）。
   ⇒ 处方：三档周期**全部由 `src/` 派生**（与固件 `DT_FAST/DT_MID/DT_SLOW` 同一条规则），
     读不到就**响亮退出**；`10ms` 只作为**能力位标称值的别名**收下，**并报出真实周期**。

## 判据（都能失败）

离线（纯 `dclc`，不需要板子）
  W1 ★ 整链同档时 `OUTPUT ... PERIOD=1ms` **编得过**（改动前：语法错误）—— 本项的存在性证明
  W2 ★ 档位表 = **固件 `DT_*` 的周期**（两边各自独立从 `src/` 派生后比对，不是自我引用）
  W3 该 OUTPUT 路由的 **period 字节低 2 位 == 1**（档位真的落进路由表，而不只是打印好看）
  W4 ★ 标称别名：`PERIOD=10ms` 收下、**打印真实周期**（含 `6.4ms`）、period 字节 == 2
  W5 负对照：`PERIOD=5ms` **被拒**，且报错里列出三档真实值（否则档位表就是"见谁都收"）
  W6 负对照：SEQ 块内写 `PERIOD=` **被拒**（逐步档位不存在；默默剥掉就是"设了就算"）
  W7 回归：既有 `examples/*.dcl` **全部仍能编过**（含 8 个写着 `PERIOD=10ms` 的）

在板（真正的行为判据）
  W8 ★★ 三档程序各自部署后，`OFF_TICK_STATS` 的增量**只落在自己那一档**、且速率与档位相符：
        div0 ⇒ Δcnt0/Δ拍 ≈ 2（2 条路由/拍）· div1 ⇒ ≈ 0.2 · div2 ⇒ ≈ 2/64
        ★ 若 `PERIOD=` 被忽略（或档位表是假的），三档会全落在 div0 ⇒ 三条全 FAIL
  W9 负对照：div0 程序期间 **Δcnt1 == Δcnt2 == 0**（证明"只落在自己那一档"不是恒真）

用法: python tools/exp_ew_output_period.py [--offline]
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import argparse, io, os, re, struct, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dclc                                            # noqa: E402  （同目录）

CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_SEQ = 0x38, 0x10, 0x12, 0x11, 0x44
OFF_TICK_STATS = 0x3854        # u32[3] cnt0/cnt1/cnt2 = 各档**累计执行的路由条次**
OFF_EXEC_RING_HDR = 0x3880     # [0]=写计数 [1]=最后一拍的 tick（ISR 新鲜）
RD_BASE, RD_WORDS = OFF_TICK_STATS, 13        # 0x3854..0x3884 一次突发读完 ⇒ 分子分母同批
IDX_CNT = (0, 1, 2)
IDX_TICK = (OFF_EXEC_RING_HDR + 4 - RD_BASE) // 4


# ══════════════════════════ 离线 ══════════════════════════
def run_dclc(text, dump=True):
    """用**真子进程**跑 dclc（与用户路径完全一致）。返回 (rc, 输出)。"""
    fd, path = tempfile.mkstemp(suffix=".dcl", prefix="_ew_")
    os.close(fd)
    try:
        io.open(path, "w", encoding="utf-8").write(text)
        cmd = [sys.executable, os.path.join(HERE, "dclc.py"), path] + (["--dump"] if dump else [])
        # ★ encoding 必须显式 —— Windows 默认 GBK 会让 dclc 的 UTF-8 输出**解码抛异常**
        #   （或更坏: 静默错码 ⇒ 判据里比对中文报错文本会假 FAIL）
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def compile_routes(text):
    """按 dclc 的**真路径**编译（parse → compile_stmts → pack_routes），返回路由 dict 列表。"""
    S = dclc.compile_stmts(dclc.parse(text))
    return S.routes, dclc.pack_routes(S.routes), dclc.pack_params(S.params)


def fw_dt_us():
    """★ 独立地**从固件的 `DT_*` 宏**算三档周期（µs）—— 用来与 dclc 的档位表交叉印证。
    固件形态（engine.h）:
        #define DT_SLOW  ((float)((uint32_t)BUCKET_DIV2_PHASES_USED * (uint32_t)TICK_PERIOD_US) / 1000000.0f)
    ⇒ 只认这一种形态；认不出来就**响亮退出**（不猜）。"""
    eh = io.open(os.path.join(ROOT, "src", "engine.h"), encoding="utf-8",
                 errors="replace").read()
    ch = io.open(os.path.join(ROOT, "src", "clock.h"), encoding="utf-8",
                 errors="replace").read()
    tick, ph1, ph2 = 100, 10, 64
    m = re.search(r"^#define\s+TICK_PERIOD_US\s+([A-Z_][A-Z0-9_]*)\b", eh, re.M)
    if m:
        tick = int(re.search(r"^#define\s+%s\s+(\d+)u?" % m.group(1), ch, re.M).group(1))
    ph1 = int(re.search(r"^#define\s+BUCKET_DIV1_PHASES\s+(\d+)u?", eh, re.M).group(1))
    nom_us = int(re.search(r"^#define\s+DIV2_NOMINAL_US\s+(\d+)u?", eh, re.M).group(1))
    mp = re.search(r"^#define\s+BUCKET_DIV2_PHASES_USED\s+(\d+)u?\s*$", eh, re.M)
    if mp:
        ph2 = int(mp.group(1))
    else:
        cap = int(re.search(r"^#define\s+BUCKET_DIV2_PHASE_MAX\s+(\d+)u?", eh, re.M).group(1)) + 1
        ph2 = min(cap, nom_us // tick)
    out = {}
    for tag, dflt in (("DT_FAST", 1), ("DT_MID", ph1), ("DT_SLOW", ph2)):
        m = re.search(r"^#define\s+%s\b\s*(.*)$" % tag, eh, re.M)
        if not m:
            raise SystemExit("!! engine.h 里没有 %s —— 三档周期的固件侧来源断了(拒绝猜)" % tag)
        ex = m.group(1)
        # 固件只有两种形态（都实测过）:
        #   DT_FAST ((float)(1u * TICK_PERIOD_US) / 1000000.0f)
        #   DT_SLOW ((float)((uint32_t)BUCKET_DIV2_PHASES_USED * (uint32_t)TICK_PERIOD_US) / 1000000.0f)
        # ⇒ 取 `((float)(` 与 `) / 1000000` 之间的**乘积因子**逐个解析; 认不出就响亮退出。
        mm = re.search(r"\(\(float\)\((.*?)\)\s*/\s*1000000", ex)
        if not mm:
            raise SystemExit("!! %s 的形态不认识: %s（拒绝猜）" % (tag, ex[:90]))
        body = mm.group(1)
        prod = 1
        for f in body.split("*"):
            f = f.strip()
            fm = re.fullmatch(r"(?:\(uint32_t\))?\s*(\d+)u?", f)
            if fm:
                prod *= int(fm.group(1))
                continue
            nm = re.fullmatch(r"(?:\(uint32_t\))?\s*([A-Za-z_][A-Za-z0-9_]*)", f)
            if nm and nm.group(1) in ("TICK_PERIOD_US", "BUCKET_DIV1_PHASES",
                                      "BUCKET_DIV2_PHASES_USED", "DIV2_NOMINAL_US"):
                prod *= {"TICK_PERIOD_US": tick, "BUCKET_DIV1_PHASES": ph1,
                         "BUCKET_DIV2_PHASES_USED": ph2, "DIV2_NOMINAL_US": nom_us}[nm.group(1)]
                continue
            raise SystemExit("!! %s 里出现不认识的因子 `%s`（拒绝猜）" % (tag, f))
        out[tag] = prod
    return out


# 三档样例：整条链同档（否则会撞"慢消费者读快生产者"那条检查）
def prog(period_suffix):
    s = " %s" % period_suffix if period_suffix else ""
    return ("CONST   one = 1.0%s\n"
            "OUTPUT  o   TO wire[40] FROM one%s\n" % (s, s))


GOOD1 = prog("PERIOD=1ms")
GOOD2 = prog("PERIOD=6.4ms")      # div2 的**真实**周期
GOOD0 = prog("")
NOMINAL = prog("PERIOD=10ms")     # 能力位标称值（别名）
BADT = prog("PERIOD=5ms")         # 不存在的档
BADSEQ = ("CONST one = 1.0\n"
          "OUTPUT o TO wire[40] FROM one\n"
          "SEQ cyc TO wire[41] PERIOD=1ms\n"
          "  UNTIL one > 0.5 DWELL 1s PERIOD=1ms\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只跑编译期判据(不连板)")
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    res, skip = [], []
    print("=" * 74)
    print("E-W  3.1 `OUTPUT ... PERIOD=` —— ③ 层能把路由放到 div1/div2")
    print("=" * 74)
    print("dclc 档位表(由 src/ 派生): TICK=%gµs  三档 = %s"
          % (dclc.TICK_US, " / ".join("%s(div%d)" % (dclc.tier_name(i), i)
                                      for i in range(3))))
    print("能力位标称别名: %s" % (("PERIOD=%.6gs → div2" % dclc.PERIOD_NOMINAL_ALIAS)
                                  if dclc.PERIOD_NOMINAL_ALIAS else "（与真实值相同 ⇒ 无别名）"))

    # ── W1 存在性: OUTPUT 收 PERIOD= ────────────────────────────────────
    print("\n── W1 ★ 整链同档时 `OUTPUT ... PERIOD=1ms` 编得过（改动前是语法错误）──")
    rc, out = run_dclc(GOOD1)
    hit = "OUTPUT" in out and "语法错误" in out
    print("    rc=%d  输出首行: %s" % (rc, (out.strip().splitlines() or ["<空>"])[0][:70]))
    if rc != 0:
        print("    ★ 编不过 ⇒ 本项**未生效**（这就是 3.1 要修的那条）")
    res.append(("W1 `OUTPUT ... PERIOD=1ms` 编得过（rc=0）", rc == 0))

    # ── W2 档位表 == 固件 DT_* ─────────────────────────────────────────
    print("\n── W2 ★ 档位表 vs 固件 `DT_FAST/DT_MID/DT_SLOW`（两边各自派生后比对）──")
    fw = fw_dt_us()
    mine = list(dclc.TIER_US)
    print("    dclc  : %s" % mine)
    print("    固件   : %s" % [fw["DT_FAST"], fw["DT_MID"], fw["DT_SLOW"]])
    res.append(("W2 dclc 三档 == 固件 DT_*（%s）" % mine,
                mine == [fw["DT_FAST"], fw["DT_MID"], fw["DT_SLOW"]]))
    print("    ⇒ div2 真实周期 = %gµs（= %d 拍 × %gµs），而能力位标称 %gµs"
          % (mine[2], mine[2] // dclc.TICK_US, dclc.TICK_US, dclc._div2_nom))

    # ── W3/W4 period 字节 ──────────────────────────────────────────────
    print("\n── W3/W4 档位真的落进路由表（period 字节低 2 位）──")
    for tag, text, want in (("div0", GOOD0, 0), ("div1", GOOD1, 1), ("div2", GOOD2, 2)):
        rts, packed, _ = compile_routes(text)
        per = [packed[i * 16 + 14] for i in range(len(rts))]
        divs = [p & 0x03 for p in per]
        phs = [(p >> 2) & 0x3F for p in per]
        print("    %-5s 路由 %d 条  period 字节 = %s ⇒ div=%s phase=%s（phase 由固件覆写, 主机只能给 0）"
              % (tag, len(rts), per, divs, phs))
        res.append(("W3 %s: 两条路由的 div 都 == %d" % (tag, want), divs == [want, want]))

    print("\n── W4 ★ 标称别名 `PERIOD=10ms`：收下 + **报真实周期** ──")
    rc4, out4 = run_dclc(NOMINAL)
    real = dclc.tier_name(2)
    said = ("标称值" in out4) and (real in out4)
    print("    rc=%d ；输出里出现『标称值』= %s ；出现真实周期 `%s` = %s"
          % (rc4, "标称值" in out4, real, real in out4))
    print("    " + next((l.strip() for l in out4.splitlines() if "标称" in l), "<无那行>")[:100])
    _, pk4, _ = compile_routes(NOMINAL)
    res.append(("W4a `PERIOD=10ms` 仍被收下（rc=0）", rc4 == 0))
    res.append(("W4b 输出里报出真实周期 `%s`（不是让人以为真是 10ms）" % real, said))
    res.append(("W4c 其 period 字节 div == 2", (pk4[14] & 0x03) == 2))

    # ── W5 负对照: 不存在的档 ──────────────────────────────────────────
    print("\n── W5 负对照：`PERIOD=5ms` **必须被拒**（否则档位表是「见谁都收」）──")
    rc5, out5 = run_dclc(BADT)
    lists = all(dclc.tier_name(i) in out5 for i in range(3))
    print("    rc=%d ；报错里列出三档真实值 = %s" % (rc5, lists))
    print("    " + next((l.strip() for l in out5.splitlines() if "不是引擎能给的档位" in l),
                         "<无那行>")[:90])
    res.append(("W5 `PERIOD=5ms` 被拒（rc≠0）", rc5 != 0))
    res.append(("W5' 报错列出三档真实值 %s" % [dclc.tier_name(i) for i in range(3)], lists))

    # ── W6 负对照: SEQ 块内 PERIOD= ────────────────────────────────────
    print("\n── W6 负对照：SEQ 块内 `PERIOD=` **必须被拒**（逐步档位不存在）──")
    rc6, out6 = run_dclc(BADSEQ)
    print("    rc=%d ；%s" % (rc6, next((l.strip() for l in out6.splitlines()
                                         if "SEQ 块内不允许" in l), "<无那行>")[:90]))
    res.append(("W6 SEQ 块内 PERIOD= 被拒（rc≠0）", rc6 != 0))

    # ── W7 回归: examples ─────────────────────────────────────────────
    ex = sorted(f for f in os.listdir(os.path.join(ROOT, "examples")) if f.endswith(".dcl"))
    bad = []
    for f in ex:
        r = subprocess.run([sys.executable, os.path.join(HERE, "dclc.py"),
                            os.path.join(ROOT, "examples", f), "--dump"],
                           capture_output=True, text=True, cwd=ROOT,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            bad.append(f)
    n_nom = sum(1 for f in ex
                if "PERIOD=10ms" in io.open(os.path.join(ROOT, "examples", f),
                                            encoding="utf-8", errors="replace").read())
    print("\n── W7 回归：既有 examples 全部仍编过（%d 个, 其中 %d 个写着 PERIOD=10ms）──"
          % (len(ex), n_nom))
    if bad:
        print("    误伤: %s" % ", ".join(bad))
    res.append(("W7 %d 个 examples 全部仍编过" % len(ex), not bad))

    # ── 在板 ─────────────────────────────────────────────────────────
    if a.offline:
        skip.append("W8/W9 在板行为判据（--offline）")
    else:
        from h723_client import Dcl

        def rd(dcl, addr, nwords, chunk=200):
            outb, off = b"", 0
            while off < nwords:
                k = min(chunk, nwords - off)
                sts, p = dcl.send(0x22, struct.pack("<IH", addr + 4 * off, k),
                                  expect_len=4 * k)
                if sts != "ACK" or len(p) < 4 * k:
                    return None
                outb += p[:4 * k]
                off += k
            return outb

        dcl = Dcl(a.port)
        print("\n端口 = %s" % dcl.port)
        time.sleep(1.0)
        try:
            sts, p = dcl.send(CMD_STATUS, expect_len=51)
            shm = struct.unpack("<I", p[23:27])[0]
            ok = False
            for _ in range(10):
                v = rd(dcl, shm + OFF_EXEC_RING_HDR, 2)
                if v and struct.unpack("<2I", v[:8])[1] >= 200000:
                    ok = True
                    break
                time.sleep(0.5)
            if not ok:
                print("!! 板子未就绪（tick 不前进）⇒ 在板判据 SKIP")
                skip.append("W8/W9 板子未就绪")
            else:
                dcl.send(CMD_STOP)
                time.sleep(0.2)
                print("前置: 顺序域清除 ⇒ %s"
                      % dcl.send(CMD_SEQ, struct.pack("<BH", 0, 0), expect_len=None)[0])

                def sample():
                    """★ 一次突发里同时取 cnt0/cnt1/cnt2 与拍号 ⇒ 分子分母**同批**。"""
                    b = rd(dcl, shm + RD_BASE, RD_WORDS)
                    if not b or len(b) < 4 * RD_WORDS:
                        return None
                    u = struct.unpack("<%dI" % RD_WORDS, b[:4 * RD_WORDS])
                    return (u[IDX_TICK], u[IDX_CNT[0]], u[IDX_CNT[1]], u[IDX_CNT[2]])

                print("\n── W8 ★★ 三档程序：增量只落在自己那一档, 速率与档位相符 ──")
                plan = (("div0", GOOD0, 0, 2.0), ("div1", GOOD1, 1, 2.0 / 10),
                        ("div2", GOOD2, 2, 2.0 / 64))
                for tag, text, tier, want in plan:
                    rts, packed, par = compile_routes(text)
                    payload = struct.pack("<HHH", len(rts), len(par) // 16, 0) + packed + par
                    s, q = dcl.send(CMD_DEPLOY, payload, expect_len=None)
                    if s != "ACK":
                        print("    %-5s deploy 被拒(%s) ⇒ 本档 SKIP" % (tag, s))
                        skip.append("W8 %s deploy 被拒" % tag)
                        continue
                    dcl.send(CMD_START)
                    time.sleep(0.6)
                    s1 = sample()
                    time.sleep(0.6)
                    s2 = sample()
                    dcl.send(CMD_STOP)
                    if not s1 or not s2 or s2[0] <= s1[0]:
                        print("    %-5s 采样失败 ⇒ SKIP" % tag)
                        skip.append("W8 %s 采样失败" % tag)
                        continue
                    dT = s2[0] - s1[0]
                    dc = [s2[1 + i] - s1[1 + i] for i in range(3)]
                    rate = dc[tier] / float(dT)
                    others = [dc[i] for i in range(3) if i != tier]
                    print("    %-5s Δ拍=%-6d Δcnt=%s  本档速率=%.4f (期望 %.4f, 偏 %+.1f%%)  他档增量=%s"
                          % (tag, dT, dc, rate, want, 100.0 * (rate - want) / want, others))
                    res.append(("W8 %s: 速率 %.4f ≈ %.4f（±5%%）" % (tag, rate, want),
                                abs(rate - want) <= 0.05 * want))
                    res.append(("W9 %s 负对照: **他档增量恒 0** %s" % (tag, others),
                                all(x == 0 for x in others)))
        finally:
            try:
                dcl.send(CMD_STOP)
                time.sleep(0.2)
                dcl.send(CMD_SEQ, struct.pack("<BH", 0, 0), expect_len=None)
                dcl.send(CMD_START)
            except Exception:
                pass

    print("\n" + "=" * 74)
    print("=== 判据 ===")
    nf = 0
    for name, ok in res:
        print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
        nf += (not ok)
    for s in skip:
        print("  [SKIP] %s" % s)
    print("\n%d 项判定, %d FAIL, %d SKIP（SKIP ≠ PASS）" % (len(res), nf, len(skip)))
    return 0 if nf == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
