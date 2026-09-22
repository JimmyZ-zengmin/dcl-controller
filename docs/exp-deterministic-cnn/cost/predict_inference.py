#!/usr/bin/env python3
"""确定性推理成本模型 —— 给定拓扑算最坏周期，并在设计空间里扫成本常数。

★ 单位：0x38 的 emax 单位是 TB（TIM5 @200MHz，1 TB = 5 ns）；TB × 2 = cyc @400MHz。

★ 成本模型（形制照本项目扫描段：C_other + Σ m_op×条数 + k×转变数）
    T = T_fixed                推理固有（入口/出口/清缓冲）
      + MACs       × c_mac     ★ c_mac **含操作数访存**（权重 ITCM / 激活 DTCM 零等待）
      + ops_nonMAC × c_op      池化/激活/量化 等非 MAC 逐元素操作
      + n_layers   × k_layer   ★ 层开销 —— 照 OP_TRANS_COST 先例：**纯可加不是上界**
  ⇒ 不单列"权重搬运/激活搬运"：它们的访存已落在 c_mac 的 load 槽里。
     分开列会**重复计价**（本脚本第一版就这么错过一次）。

★ 纪律：`c_mac` 在 `cost/measure_mac_cost.py` 实测之前，一律标 EST **不得引用为结论**。
"""
import argparse

F_CPU = 400_000_000
TICK_US = 100
TICK_CYC = F_CPU * TICK_US // 1_000_000          # 40000
EXEC_BUDGET_CYC = TICK_CYC * 80 // 100           # 32000

# ── 候选拓扑 ──
# 输入 = 跟随误差序列，10 kHz 采样。"C_in" 必须显式写 —— 卷积的 MAC = L_out×C_out×K×C_in
TOPOS = {
    "A 方案原样":      dict(L=256, C1=8,  K1=5, P1=4, C2=16, K2=5, NCLS=3),
    "B 瘦身(pool2)":   dict(L=256, C1=8,  K1=5, P1=4, C2=12, K2=3, NCLS=3),
    "C 短窗 128":      dict(L=128, C1=8,  K1=5, P1=4, C2=16, K2=5, NCLS=3),
    "D 最小可用":      dict(L=128, C1=6,  K1=3, P1=4, C2=8,  K2=3, NCLS=3),
}


def derive(t):
    L1 = t["L"] - t["K1"] + 1
    L1P = L1 // t["P1"]
    L2 = L1P - t["K2"] + 1
    mac1 = L1 * t["C1"] * t["K1"] * 1                        # C_in = 1
    mac2 = L2 * t["C2"] * t["K2"] * t["C1"]                  # ★ C_in = C1,不能漏
    macfc = t["C2"] * t["NCLS"]
    pool = (L1 * t["C1"]) + (L1P * t["C2"])                    # 逐元素比较的规模
    gap = t["C2"] * L2
    params = (t["C1"] * t["K1"] + t["C1"]) + (t["C2"] * t["K2"] * t["C1"] + t["C2"]) \
             + (t["C2"] * t["NCLS"] + t["NCLS"])
    buf = dict(inp=t["L"], conv1=L1 * t["C1"], pool1=L1P * t["C1"],
               conv2=L2 * t["C2"], pool2=L2 * t["C2"])
    return dict(L1=L1, L1P=L1P, L2=L2, mac1=mac1, mac2=mac2, macfc=macfc,
                mac=mac1 + mac2 + macfc, nonmac=pool + gap, params=params,
                arena=max(buf.values()) * 2, buf=buf)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--klayer", type=float, default=80.0, help="层开销 cyc/层")
    p.add_argument("--tfixed", type=float, default=400.0)
    p.add_argument("--cop", type=float, default=1.0, help="非 MAC 逐元素 cyc")
    p.add_argument("--nlayer", type=int, default=4, help="conv/pool/conv/gap+fc 计 4~5 层")
    p.add_argument("--slice", type=int, default=8000)
    a = p.parse_args()

    print("=" * 96)
    print("DT-CNN 成本模型 · 设计空间   |   T = T_fixed + MAC×c_mac + 非MAC×c_op + n_layer×k_layer")
    print("=" * 96)
    print("  T_fixed=%.0f  c_op=%.1f  k_layer=%.0f  n_layer=%d   拍长=%d cyc  推理切片=%d cyc"
          % (a.tfixed, a.cop, a.klayer, a.nlayer, TICK_CYC, a.slice))

    for name, t in TOPOS.items():
        d = derive(t)
        print("\n" + "-" * 96)
        print("【%s】 输入 %d 点 @10kHz = %.1f ms 窗" % (name, t["L"], t["L"] / 10))
        print("  形状 : conv1 %d×%d(k%d) → pool%d → conv2 %d×%d(k%d) → GAP → FC %d"
              % (d["L1"], t["C1"], t["K1"], t["P1"], d["L2"], t["C2"], t["K2"], t["NCLS"]))
        print("  MAC  : conv1 %6d (L%d×C%d×K%d×Cin1) + conv2 %6d (L%d×C%d×K%d×Cin%d) + fc %3d = %6d"
              % (d["mac1"], d["L1"], t["C1"], t["K1"],
                 d["mac2"], d["L2"], t["C2"], t["K2"], t["C1"], d["macfc"], d["mac"]))
        print("  非MAC: %5d   参数 %4d 个 (int8 %.2f KB)   arena %d B" %
              (d["nonmac"], d["params"], d["params"] / 1024, d["arena"]))
        print("  %-12s %10s %10s %12s %10s" % ("c_mac", "总周期", "µs", "占拍", "拍数"))
        for cmac, tag in ((0.5, "int8+DSP 乐观"), (1.0, "int8 实测中位"), (2.0, "int8 保守"), (4.0, "f32")):
            T = a.tfixed + d["mac"] * cmac + d["nonmac"] * a.cop + a.nlayer * a.klayer
            print("  %5.1f cyc/MAC %10.0f %10.1f %11.1f%% %9s   ← %s"
                  % (cmac, T, T / 4 / 1000, 100 * T / TICK_CYC,
                     "1 拍" if T <= TICK_CYC else "%d 拍" % -(-int(T) // TICK_CYC), tag))

    print("\n" + "=" * 96)
    print("★ 以上 c_mac 全部是 **EST**（预估）。实测入口: cost/measure_mac_cost.py")
    print("★ 对账口径: 推理切片 + 引擎执行判据(%d) ≤ 拍长(%d)" % (EXEC_BUDGET_CYC, TICK_CYC))
    print("★ 层开销项不是装饰: 本项目 OP_TRANS_COST 就是「纯可加不是上界」被抓到之后补的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
