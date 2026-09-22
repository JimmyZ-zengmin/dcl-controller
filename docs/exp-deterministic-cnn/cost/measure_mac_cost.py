#!/usr/bin/env python3
"""Q1 —— 上机标定 `cycles/MAC`（DT-CNN 的承重常数）

## 为什么需要这个脚本
`c_mac` 是全部结论的支点：它决定"能不能进一拍"。而它**只能实测** ——
本项目的先例是 `k_op_cost_itcm[]`（19 原语逐条实测 + 留点验证），不是模型推出来的。

## 固件侧需要什么（**尚未实现，这是本脚本的前置**）
在 ISR 里加一个**基准 kernel 钩子**，要求：
  · 定长循环、无数据相关分支、无早退（`THEORY.md` §3 的清单）
  · 工作量由参数控制：`macs` = 本次要做的 MAC 数
  · 检测方式：与 `adc_poll` 同族的**拍内每拍推进一步的状态机**（GAP-6 推论），
    或"某拍内一次跑完"——**两种都要能选**，因为语义不同
  · 暴露点（二选一）：
      ① 复用 `0x38` 的 `emax`：跑 N 拍后读 `emax`（最省，零新协议）
      ② 新增 `0x39 op=NN`：返回 [macs, emax, t_fixed, n_run]（更好对账）

## 测量法（形制照本项目的 `C_other` 与 19 原语）
  1. `0x13 RESET` → 时基活性闸门（`pmin` 非 0，否则**拒答**）
  2. 跑 macs=0      ⇒ `T0`（固有 + 层开销）
  3. 跑 macs=K1,K2,… ⇒ `emax` 线性回归 ⇒ **斜率 = c_mac**，截距 = `T_fixed + Σk_layer`
  4. 留点验证：用一个**没参与拟合**的 `macs` 点，残差必须 ≤3%
  5. ★ 数据无关性：同一 `macs`、**两组不同输入** ⇒ `emax` 必须相同
  6. ★ 负对照：在 kernel 里插一条数据相关早退 ⇒ 第 5 条**必须变红**
  7. ★ FZ 对照：关掉 `FPSCR.FZ` 的一档 ⇒ 与开启档**必须有差异**（否则那条断言是空的）

用法:
    DCL_PORT=COM21 python docs/exp-deterministic-cnn/cost/measure_mac_cost.py --dry
    DCL_PORT=COM21 python docs/exp-deterministic-cnn/cost/measure_mac_cost.py --macs 1000,2000,4000,8000
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

OP_CMD_REQ, CMD_STATUS = 0x39, 0x38      # 0x39 是诊断脚手架；0x38 有 emax
TICK_TB = 20000                          # 100 µs @200MHz(TIM5)


def liveness(dcl):
    """★ 任何计时结论的前置：时基必须活着，否则拒答（本项目铁律）。"""
    import struct, time
    dcl.send(0x13); time.sleep(0.3)
    dcl.send(0x11); time.sleep(1.2)
    for _ in range(4):
        s, p = dcl.send(CMD_STATUS, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            pmin = struct.unpack("<I", p[4:8])[0]
            return pmin
        time.sleep(0.2)
    return 0


def read_status(dcl):
    import struct, time
    for _ in range(4):
        s, p = dcl.send(CMD_STATUS, expect_len=51)
        if s == "ACK" and len(p) >= 51:
            d = struct.unpack("<IIIII", p[:20])
            return dict(emax=d[4], emin=d[3], ov=struct.unpack("<I", p[27:31])[0], pmin=d[1])
        time.sleep(0.2)
    return None


def fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    icept = my - slope * mx
    ss_res = sum((y - (icept + slope * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return slope, icept, r2


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--macs", default="0,1000,2000,4000,8000,16000")
    p.add_argument("--holdout", type=int, default=12000, help="留点验证用的 MAC 数（不参与拟合）")
    p.add_argument("--dry", action="store_true", help="只打印计划，不碰板子")
    a = p.parse_args()
    macs = [int(x) for x in a.macs.split(",")]

    print("=" * 78)
    print("Q1 · cycles/MAC 标定计划")
    print("=" * 78)
    print("\n前置（★ 固件侧尚未实现）:")
    print("  · 拍内基准 kernel：定长循环 / 无分支 / 无早退（THEORY.md §3）")
    print("  · 工作量为参数 `macs`；暴露 emax（复用 0x38 最省）")
    print("\n测量点:")
    print("  %-10s %s" % ("macs", "用途"))
    for m in macs:
        print("  %-10d %s" % (m, "基线 T_fixed+Σk_layer" if m == 0 else "拟合点"))
    print("  %-10d **留点验证**（不参与拟合，残差须 ≤3%%）" % a.holdout)

    if a.dry:
        print("\n[dry] 未连接板子。")
        return 0

    from h723_client import Dcl
    dcl = Dcl(os.environ.get("DCL_PORT") or "COM21")
    print("\n端口 = %s" % dcl.port)
    try:
        pmin = liveness(dcl)
        print("★ 时基活性: pmin = %d %s" % (pmin, "OK" if pmin > 0 else "**死 ⇒ 拒答**"))
        if pmin == 0:
            print("  ⇒ 判 FAIL，不产出任何 c_mac。")
            return 1

        ys, xs = [], []
        for m in macs:
            # 固件钩子：0x39 op=NN 设 macs 并跑；此处用 0x38 读 emax 对账
            import struct, time
            dcl.send(OP_CMD_REQ, struct.pack("<BI", 0x99, m), expect_len=None)
            time.sleep(0.5)
            s = read_status(dcl)
            if not s:
                print("  macs=%-6d <读不到 0x38>" % m)
                continue
            print("  macs=%-6d emax=%-7d TB (%.1f µs)  ov=%d" % (m, s["emax"], s["emax"] * 5 / 1000, s["ov"]))
            xs.append(m)
            ys.append(s["emax"] * 2)      # TB → cyc（×2 @400MHz）

        if len(xs) < 3:
            print("\n⇒ 拟合点不足（<3）⇒ 判 SKIP，不产出 c_mac。")
            return 1
        slope, icept, r2 = fit(xs, ys)
        print("\n=== 拟合 ===")
        print("  c_mac   = %.3f cyc/MAC   (R² = %.5f)" % (slope, r2))
        print("  T_fixed + Σk_layer = %.0f cyc" % icept)
        print("\n★ 填入 kernels/dcnn_topology.h：")
        print("    -DDC_C_MAC_Q8=%d -DDC_K_LAYER_CYC=? -DDC_T_FIXED_CYC=?" % round(slope * 8))
        print("\n★ 还没做的三条（不做完，这个 c_mac 不足以支撑『可证』）：")
        print("    ① 留点验证 macs=%d，残差 ≤3%%" % a.holdout)
        print("    ② 数据无关性：同一 macs、两组不同输入 ⇒ emax 必须相同")
        print("    ③ FZ 对照：关掉 FPSCR.FZ 的一档必须量出差异")
    finally:
        try:
            dcl.send(0x12); dcl.send(0x13)
        except Exception:
            pass
        dcl.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
