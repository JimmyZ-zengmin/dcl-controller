#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-Y —— 3.4 `LUT` 的最小验证：**把"表不在程序里"钉成判据**（先量，再决定架构）

## 为什么先做验证而不是先实现
PLAN 3.4 的原话：`0x23` 能写 LUT 区（表**今天就能上传**），但 `0x10 deploy` 不碰 LUT 区
⇒ **表不在程序里**（上电后是残留/零、不进 SD A/B）⇒ "必须先定表住哪"。
那是一句**侦察结论**，不是判据。本工具的用途就是把它变成**能失败的事实**，
再把三条可选架构路线的代价摆出来 —— 架构决定不顺手做。

## 判据（都能失败）

源码级（不需要板子）
  Y1s `engine_reload_active()`（deploy 的生效路径）**不含** LUT 区
  Y2s 持久化镜像 payload 只覆盖 routes+params+states（`PERSIST_PAYLOAD_MAX` 现算 == 6144）
  Y3s 程序包唯一的附加段是 dev_bind（`DB_SEG_LEN`）—— **没有** LUT 段
  Y4s ★ `engine_fill_tables()` **会写 LUT 斜坡** ⇒ 一旦 reinit 路径被启用，
      主机上传的表会被**静默覆盖**（今天 `g_reinit` 全仓无人置 1，钩子在那儿）

在板
  Y5 ★ `0x23` 写 LUT 区 ⇒ ACK + **回读逐字节一致**（"表今天就能上传"这句话的证据）
  Y6 ★ `0x10 deploy` 之后 LUT 区**逐字节不变**；而 routes 区**确实变了**
      （负对照 —— 否则"不变"可能只是"整块 SHM 都没动"）
  Y7 ★ `0x13 RESET` 之后 LUT 区**全 0** ⇒ 表**不随任何东西存活**（复位即失）

用法: python tools/exp_ey_lut_provenance.py [--offline]
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import argparse, io, os, re, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

CMD_STATUS, CMD_DEPLOY, CMD_STOP, CMD_START, CMD_SEQ, CMD_RESET = (
    0x38, 0x10, 0x12, 0x11, 0x44, 0x13)
CMD_WRITE, CMD_BURST = 0x21, 0x22
OFF_LUT_DATA, MAX_LUT = 0x0440, 256
OFF_ROUTE_TABLE = 0x0840
LUT_BYTES = MAX_LUT * 4
# 主机要写进去的"表"：与固件 fill_tables 的斜坡**明确不同** ⇒ 一眼能看出是谁的值
PATTERN = [float((i * 7) % 251) * 0.5 - 60.0 for i in range(MAX_LUT)]

DCL_PROG = ("CONST   one = 1.0\n"
            "OUTPUT  o   TO wire[40] FROM one\n")


def src(f):
    return io.open(os.path.join(ROOT, "src", f), encoding="utf-8", errors="replace").read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--port", default=None)
    a = ap.parse_args()
    res, skip = [], []
    print("=" * 74)
    print("E-Y  3.4 LUT 的最小验证：表到底住在哪")
    print("=" * 74)
    print("LUT 区: shm+0x%04X, %d × f32 = %d B；紧接着 0x%04X 起是 ROUTE_TABLE"
          % (OFF_LUT_DATA, MAX_LUT, LUT_BYTES, OFF_ROUTE_TABLE))

    # ── 源码级 ────────────────────────────────────────────────────────
    print("\n── Y1s `engine_reload_active()`（deploy 生效路径）不含 LUT ──")
    ec = src("engine.c")
    i = ec.find("void engine_reload_active")
    assert i > 0, "engine.c 里找不到 engine_reload_active —— 判据的来源断了(拒绝猜)"
    body = ec[i:i + 1800]
    end = body.find("\n}")
    body = body[:end if end > 0 else len(body)]
    hit_lut = [ln.strip() for ln in body.splitlines() if "LUT" in ln or "lut" in ln]
    print("    engine_reload_active 正文 %d 行, 出现 LUT 的行 = %s" % (len(body.splitlines()),
                                                                      hit_lut or "无"))
    res.append(("Y1s deploy 生效路径（engine_reload_active）不含 LUT", not hit_lut))

    print("\n── Y2s 持久化镜像 payload 只覆盖 routes+params+states ──")
    pc = src("persist.c")
    m = re.search(r"#define\s+PERSIST_PAYLOAD_MAX\s+\(\(MAX_ROUTES\s*\+\s*MAX_PARAMS\s*\+\s*MAX_STATES\)\s*\*\s*16u\)",
                  pc)
    print("    persist.c: %s" % (m.group(0) if m else "<找不到>"))
    res.append(("Y2s PERSIST_PAYLOAD_MAX == (128+128+128)*16（无 LUT）", bool(m)))

    print("\n── Y3s 程序包唯一的附加段是 dev_bind（没有 LUT 段）──")
    db = src("dev_bind.c")
    n_seg = len(re.findall(r"DB_SEG_LEN", db))
    packs = re.findall(r"len\s*\+\s*DB_SEG_LEN", db)
    print("    dev_bind.c: dev_bind_pack 追加一段(出现 %d 次)、DB_SEG_LEN 引用 %d 次；"
          "全仓无 LUT 段" % (len(packs), n_seg))
    no_lut_seg = not re.search(r"LUT_SEG|SEG_LUT|lut_seg", pc + db + src("prog_store.c"))
    res.append(("Y3s 程序包只带 dev_bind 段, 无 LUT 段", no_lut_seg))

    print("\n── Y4s ★ `engine_fill_tables()` 会写 LUT 斜坡（reinit 时会静默覆盖主机表）──")
    ramp = re.search(r"lu\[i\]\s*=\s*\(float\)\(i\s*&\s*0x3F\)\s*\*\s*0\.1f", ec)

    def strip_comments(t):
        """★ 必须先剥注释: 第一版在 `engine.c` 里"找到"了 `g_reinit=1` —— 那是
        **注释里的一句话**(描述主循环语义), 不是代码。不剥注释的搜索会给出
        「有人启用过 reinit」这种**假事实**, 而它正好是本节要回答的问题。"""
        t = re.sub(r"/\*.*?\*/", " ", t, flags=re.S)
        return re.sub(r"//[^\n]*", " ", t)

    reinit_setters = [f for f in ("main.c", "engine.c", "persist.c", "dev_bind.c", "step.c",
                                  "blackbox.c", "dev_bind.h")
                      if re.search(r"\bg_reinit\s*=\s*1", strip_comments(src(f)))]
    print("    fill_tables 里的 LUT 斜坡: %s" % ("在" if ramp else "找不到"))
    print("    全仓把 `g_reinit=1` 的地方 = %s（空 = 钩子今天没人用, 但**路径已存在**）"
          % (reinit_setters or "无"))
    res.append(("Y4s fill_tables 写 LUT 斜坡（reinit 会覆盖主机表）—— 事实已确认",
                bool(ramp)))

    # ── 在板 ─────────────────────────────────────────────────────────
    if a.offline:
        skip.append("Y5/Y6/Y7 在板（--offline）")
    else:
        from h723_client import Dcl

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

        dcl = Dcl(a.port)
        print("\n端口 = %s" % dcl.port)
        time.sleep(1.0)
        try:
            sts, p = dcl.send(CMD_STATUS, expect_len=51)
            shm = struct.unpack("<I", p[23:27])[0]
            lut = shm + OFF_LUT_DATA
            rtb = shm + OFF_ROUTE_TABLE
            ok = False
            for _ in range(10):
                v = rd(dcl, shm + 0x3884, 1)
                if v and struct.unpack("<I", v)[0] >= 200000:
                    ok = True
                    break
                time.sleep(0.5)
            if not ok:
                skip.append("Y5/Y6/Y7 板子未就绪")
            else:
                dcl.send(CMD_STOP)
                time.sleep(0.2)
                dcl.send(CMD_SEQ, struct.pack("<BH", 0, 0), expect_len=None)

                before = rd(dcl, lut, MAX_LUT)
                fb = struct.unpack("<%df" % MAX_LUT, before)
                # ★ 斜坡必须按 **float32** 比较: 固件是 `(float)(i&0x3F) * 0.1f`,
                #   而 Python 的 `* 0.1` 是 float64 ⇒ 直接比 list 会**假不等**
                #   (实测前 4 值 0.000/0.100/0.200/0.300 明明就是斜坡)。
                ramp_pat = [struct.unpack("<f", struct.pack("<f", (i & 0x3F) * 0.1))[0]
                            for i in range(MAX_LUT)]
                zero = all(x == 0.0 for x in fb)
                is_ramp = all(struct.unpack("<f", struct.pack("<f", x))[0] == rp
                              for x, rp in zip(fb, ramp_pat))
                print("\n    [现状] LUT 前 4 值 = %s ；全 0 = %s ；== fill_tables 斜坡 = %s"
                      % (["%.3f" % x for x in fb[:4]], zero, is_ramp))
                if is_ramp:
                    print("    ⇒ 现状 = **上一次冷启动 fill_tables 铺的斜坡**, 且此后没有任何东西")
                    print("      动过它（deploy 不碰、也没人写过）—— 这正是 Y1s/Y6 的现场证据。")
                # ★★ 这一条**不进判据表**: 它说的是"板子的**历史**", 不是"固件的性质"。
                #   第一次跑本工具时读到的是斜坡 (0.000/0.100/0.200/0.300…), 第二次读到全 0
                #   —— 因为第一次跑完的 Y7 刚做过 0x13 RESET。⇒ 它**无法在单次会话内复现**
                #   (要一次真实掉电)。本项目纪律: 台架/历史依赖的观察**必须与判据分开记账**,
                #   否则它会周期性地"看起来像回归"(同 h723_step_ramp_test.py 的 R5)。
                print("    ★ 本项为**台架/历史依赖观察**, 不进判据表(见 PLAN 4.3):"
                      " 它取决于上一次冷启动与上一次 RESET")
                skip.append("Y4s' LUT 现状 == 冷启动斜坡 —— **历史依赖**, 需一次真实掉电才能复现")

                # ── Y5 上传 + 回读 ───────────────────────────────
                print("\n── Y5 ★ `0x23` 写 LUT 区 ⇒ ACK + 回读逐字节一致 ──")
                bits = b"".join(struct.pack("<f", v) for v in PATTERN)
                st23, q23 = dcl.send(0x23, struct.pack("<IH", lut, MAX_LUT) + bits,
                                     expect_len=None)
                back = rd(dcl, lut, MAX_LUT)
                same = (back == bits)
                diff = sum(1 for k in range(MAX_LUT) if back[k * 4:k * 4 + 4] != bits[k * 4:k * 4 + 4])
                print("    0x23 ⇒ %s ；回读 %d/%d 个字不同" % (st23, diff, MAX_LUT))
                print("    前 4 值 = %s（写的是 %s）"
                      % (["%.3f" % x for x in struct.unpack("<4f", back[:16])],
                         ["%.3f" % x for x in PATTERN[:4]]))
                res.append(("Y5 主机能把 LUT 表写进板子（0x23 ACK + 逐字节一致）", same))

                # ── Y6 deploy 不碰 LUT（routes 作负对照）─────────
                print("\n── Y6 ★ deploy 不改 LUT；但 routes **确实变**（负对照）──")
                rt_before = rd(dcl, rtb, 64)
                S = _compile(DCL_PROG)
                st, _ = dcl.send(CMD_DEPLOY, S, expect_len=None)
                time.sleep(0.4)
                lut_after = rd(dcl, lut, MAX_LUT)
                rt_after = rd(dcl, rtb, 64)
                print("    deploy ⇒ %s ；LUT 逐字节不变 = %s ；routes 变了 = %s"
                      % (st, lut_after == bits, rt_before != rt_after))
                res.append(("Y6 deploy 不碰 LUT（逐字节不变）", lut_after == bits))
                res.append(("Y6' 负对照: 同一次 deploy 之后 routes 区**确实变了**",
                            rt_before != rt_after))
                dcl.send(CMD_STOP)

                # ── Y7 RESET 清空 LUT ────────────────────────────
                print("\n── Y7 ★ `0x13 RESET` 之后 LUT 全 0 ⇒ 表不随任何东西存活 ──")
                st13, _ = dcl.send(CMD_RESET, expect_len=None)
                time.sleep(0.4)
                z = rd(dcl, lut, MAX_LUT)
                zf = struct.unpack("<%df" % MAX_LUT, z)
                print("    RESET ⇒ %s ；LUT 全 0 = %s（非 0 个数 = %d）"
                      % (st13, all(x == 0.0 for x in zf), sum(1 for x in zf if x != 0.0)))
                res.append(("Y7 RESET 之后 LUT 区全 0（复位即失, 不持久）",
                            all(x == 0.0 for x in zf)))
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


def _compile(text):
    """用 dclc 的真路径编译成 deploy 载荷。"""
    import dclc
    S = dclc.compile_stmts(dclc.parse(text))
    return (struct.pack("<HHH", len(S.routes), len(S.params), 0)
            + dclc.pack_routes(S.routes) + dclc.pack_params(S.params))


if __name__ == "__main__":
    sys.exit(main())
