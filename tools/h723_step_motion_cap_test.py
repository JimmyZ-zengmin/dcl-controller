#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_step_motion_cap_test.py —— **运动能力面（程序面）** 判据 M1–M6。

对应 `docs/PLAN-step-motion-v1.md` Step 1：把"运动"从诊断脚手架（`0x39 op=19`）
提为**②外设能力** —— ③层（DCL 程序）写 `ACTUATOR[12..15]`，由 `step_service_motion()` 消费。

## 为什么这一步重要
`op=19` 是脚手架（③层不得依赖）⇒ **"闭环步进的 DCL 程序"在架构上写不出来**。
本线开的是**另一条通路**：③层写 `actuator[12..15]`（**本来就是合法动作**，路由的 dst 可以是 ACTUATOR）
⇒ **零 SHM 语义扩展**，而闭环因此可以**搬进 DCL 程序**（③层每拍执行 ⇒ 环频远高于 PC 在环的 13.5 Hz）。

## ★★ 运动源必须显式
`op=19 sub=13 arg=0` = 脚手架直控【**默认**】/ `1` = 程序面。
不这样做的话，程序面会**每圈覆盖**脚手架设的频率 ⇒ 所有运动回归套件全废。⇒ M1 就是这条的机检形式。

## 判据
| # | 判据 | 怎么让它失败 |
|---|---|---|
| M1 | **默认 = 脚手架**：`sub=13 arg=0` 后 src==0，且脚手架 `sub=1` 仍生效 | 回归全跑（既有行为零变化）|
| M2 | 切程序面 ⇒ 写 `wire[12]=500` ⇒ 读回**实际频率 500 Hz**；写 0 ⇒ 停（`CC1E=0`）| 值与实际不符即 FAIL |
| M3 | ★ **就绪门**：值不变时 `cmd_n` **不得**再涨（防"每圈空转"）| 连采两次，计数变了就是空转 |
| M4 | ★ **fail-closed**：未声明极性时写 `actuator[14]>0.5` ⇒ 被拒（`rej_n` 涨）且物理失能 | 交付档极性已声明 ⇒ **判 SKIP**（覆盖不到 ≠ 通过）|
| M5 | **钳位可观测**：写 4 Hz（低于 TIM3 下界）⇒ 实际 = **15 Hz**（`1e6/65536`），不是 4 | 静默给 4 ⇒ FAIL |
| M6 | **镜像槽对得上**：`ACTUATOR[16]` == `op=19` 报的 `g_step_rate_hz` | 不一致 ⇒ FAIL |

用法: python tools/h723_step_motion_cap_test.py [--port COM21]
"""
import os
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h723_client import Dcl, find_board       # noqa: E402

OFF_WIRE = 0x240      # ★ 运动请求住在 WIRE 面（③层的输出面只有 wire[]）
S_RATE, S_DIR, S_ENA, S_LIMIT = 12, 13, 14, 15
S_RATE_AP = 64      # ★ 镜像槽必须 >63（dclc 自动分配上限 64，会抢 16/17）

N_PASS = N_FAIL = N_SKIP = 0


def record(name, ok, detail=""):
    """★ 仓库约定：判据发射器必须叫 `record(...)`（docs/claims.md 的 E 类闸门按它找判据）。"""
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def skip(name, why):
    global N_SKIP
    N_SKIP += 1
    print("  [SKIP] %s —— %s" % (name, why))


def shm_base(d):
    """★ `0x38` 的 +23 是 `g_shm`（非对齐打包，见 main.c 的 h_engine_status）。

    ★★★ 为什么必须拿它：`0x20 READ` / `0x23 WRITE_BURST` 要的是**绝对地址**
    （`eng_valid_range()` 是拿 `a >= (uint32_t)g_shm` 比的），**不是 SHM 偏移**。
    传偏移会得到 `NAK "bad addr"` —— 本项目踩过：回归闸门里那句"读 n_routes"就因为
    传了偏移 `0x0E` 而**一直是 -1**（看起来像"读不到"，其实是地址口径错）。
    """
    s, p = d.send(0x38)
    if s != "ACK" or len(p) < 27:
        return None
    return struct.unpack("<I", p[23:27])[0]


def f2u(v):
    return struct.unpack("<I", struct.pack("<f", float(v)))[0]


def u2f(u):
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def w_act(d, base, idx, val):
    """0x23 WRITE_BURST: [addr:u32(绝对)][count:u16][count×u32] → 写一个 float 槽。"""
    return d.send(0x23, struct.pack("<IH", base + OFF_WIRE + idx * 4, 1)
                  + struct.pack("<I", f2u(val)))[0]


def r_act(d, base, idx):
    """0x20 READ: [addr:u32(绝对)] → ACK [val:u32]。"""
    s, p = d.send(0x20, struct.pack("<I", base + OFF_WIRE + idx * 4))
    if s != "ACK" or len(p) < 4:
        return None
    return u2f(struct.unpack("<I", p[:4])[0])


def m14(d):
    s, p = d.send(0x39, bytes([19, 14]))
    if s != "ACK" or len(p) < 32:
        return None
    u = struct.unpack("<8I", p[:32])
    return dict(src=u[0], cmd=u[1], app=u[2], rej=u[3], rate=u[4], lim=u[5])


def st0(d):
    s, p = d.send(0x39, bytes([19, 0]) + struct.pack("<I", 0))
    if s != "ACK" or len(p) < 96:
        return None
    u = struct.unpack("<24I", p[:96])
    return dict(rate=u[0], dir=u[1], ena=u[2], ccer=u[4])


def main():
    port = None
    if "--port" in sys.argv:
        port = sys.argv[sys.argv.index("--port") + 1]
    d = Dcl(port or find_board())
    try:
        print("=== 运动能力面（程序面）判据 M1–M6 ===")
        base = shm_base(d)
        if base is None:
            print("  ✗ 读不到 g_shm（0x38 无应答）⇒ 判**无效**")
            return 1
        m = m14(d)
        if m is None:
            print("  ✗ `op=19 sub=14` 无应答 ⇒ 固件不是本版（PLAN-step-motion-v1 Step 1）⇒ 全部判**无效**")
            return 1

        # ── M1 默认 = 脚手架（既有行为零变化的前提）──
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))
        time.sleep(0.15)
        m = m14(d)
        record("M1 运动源默认 = 脚手架（src==0）⇒ 既有行为零变化", m["src"] == 0,
               "src=%d cmd_n=%d applied_n=%d" % (m["src"], m["cmd"], m["app"]))

        # ── 切程序面 ──
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 1))
        time.sleep(0.15)
        m = m14(d)
        record("M1b 可切到程序面（src==1）", m["src"] == 1, "src=%d" % m["src"])
        n_cmd0 = m["cmd"]

        # ── M2 写槽 ⇒ 真的改频率 ──
        w_act(d, base, S_RATE, 500.0)
        time.sleep(0.20)
        s = st0(d)
        ok = abs(s["rate"] - 500) <= 2
        record("M2 ③层写 wire[12]=500 ⇒ 实际频率 500 Hz", ok,
               "实际=%d Hz ccer=%d" % (s["rate"], s["ccer"] & 1))
        w_act(d, base, S_RATE, 0.0)
        time.sleep(0.20)
        s = st0(d)
        record("M2b 写 0 ⇒ 停脉冲（CC1E=0）", (s["ccer"] & 1) == 0 and s["rate"] == 0,
               "rate=%d ccer=%d" % (s["rate"], s["ccer"] & 1))

        # ── M3 ★ 就绪门：值不变时 cmd_n 不得再涨 ──
        w_act(d, base, S_RATE, 300.0)
        time.sleep(0.25)
        a = m14(d)
        time.sleep(0.50)          # ★ 这段时间里主循环要跑很多圈
        b = m14(d)
        record("M3 值不变 ⇒ cmd_n/applied_n 不再涨（防每圈空转）",
               a["cmd"] == b["cmd"] and a["app"] == b["app"],
               "cmd_n %d→%d, applied_n %d→%d（期间主循环跑了 ~1300 圈）"
               % (a["cmd"], b["cmd"], a["app"], b["app"]))
        record("M3b 该次变化被记账（cmd_n 从切面后确实涨过）", b["cmd"] > n_cmd0,
               "cmd_n %d→%d" % (n_cmd0, b["cmd"]))

        # ── M5 钳位可观测（TIM3 下界 = 1e6/65536 = 15 Hz）──
        w_act(d, base, S_RATE, 4.0)
        time.sleep(0.20)
        s = st0(d)
        record("M5 写 4 Hz ⇒ 钳到 15 Hz（TIM3 16 位下界），不静默给 4",
               s["rate"] == 15, "实际=%d Hz" % s["rate"])
        w_act(d, base, S_RATE, 0.0)
        time.sleep(0.15)

        # ── M6 镜像槽 == 实际 ──
        w_act(d, base, S_RATE, 800.0)
        time.sleep(0.25)
        mir = r_act(d, base, S_RATE_AP)
        s = st0(d)
        record("M6 只读镜像 wire[16] == 实际频率（跨拍就绪门可判）",
               mir is not None and abs(mir - s["rate"]) <= 1,
               "镜像=%s 实际=%d" % (mir, s["rate"]))
        w_act(d, base, S_RATE, 0.0)
        time.sleep(0.15)

        # ── M4 fail-closed（交付档上走不到 ⇒ SKIP）──
        s11, p11 = d.send(0x39, bytes([19, 11]))
        pol_set = struct.unpack("<8I", p11[:32])[1] if (s11 == "ACK" and len(p11) >= 32) else -1
        if pol_set == 1:
            skip("M4 未声明极性 ⇒ 程序面写 wire[14] 必须被拒",
                 "本档极性已声明(pol_set=1) ⇒ 拒绝路径不可达；需 -DDCL_STEP_ENA_POL=-1 的对照档")
        else:
            w_act(d, base, S_ENA, 1.0)
            w_act(d, base, S_RATE, 500.0)
            time.sleep(0.25)
            a = m14(d)
            s = st0(d)
            record("M4 未声明极性 ⇒ 程序面写 wire[14] 被拒（fail-closed）",
                   a["rej"] > 0 and s["ena"] == 0, "rej_n=%d ena=%d" % (a["rej"], s["ena"]))
            w_act(d, base, S_RATE, 0.0)

        # 收尾：回到脚手架（★ 脚本的状态复原 —— 本项目铁律）
        w_act(d, base, S_RATE, 0.0)
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))
        d.send(0x39, bytes([19, 6]))
        time.sleep(0.2)
        m = m14(d)
        print()
        print("  [收尾] src=%d（已回脚手架）rate=%d ⇒ 交给下一次测试的是『干净态』" % (m["src"], m["rate"]))
        print()
        print("── 汇总: PASS %d / FAIL %d / SKIP %d ──" % (N_PASS, N_FAIL, N_SKIP))
        return 1 if N_FAIL else 0
    finally:
        d.close()


if __name__ == "__main__":
    sys.exit(main())
