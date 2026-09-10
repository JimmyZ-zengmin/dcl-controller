#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
h723_pad_sweep.py — ★ 落位敏感机制扫描 (AUDIT-H723-stage2.md H8)

要回答的问题:
  审计报告 §8 只能写"同一份机器码换个 flash 地址成本就变"，那是**相关性** ——
  对照组是"另一个构建"，机制未测。本脚本把落位做成**唯一变量**:

    -DSCAN_FLASH_PAD=N  →  在 engine_scan_flash 之前插入 N 字节填充
      (链接脚本 .scan_pad / .scan_flash 两个专区, 见 ld/*.ld)
    → 只挪 engine_scan_flash 的地址, **不改一条指令**

判据:
  · 控制组 engine_scan_itcm (VMA 固定 0x0) 的 cost 必须**纹丝不动** —— 否则实验无效
  · 若 FLASH 版 cost 随 (地址 mod 取指行) 呈**周期性台阶** → 取指行边界机制成立
  · 若随 N 单调/随机漂移 → 另有他因, 本报告不得写"因为取指行边界"

用法:
  python tools/h723_pad_sweep.py                       # 默认 10 个点 (~8 分钟)
  python tools/h723_pad_sweep.py --pads 0,4,8,16,32    # 自定义
  python tools/h723_pad_sweep.py --dur 0.5
"""
import os, sys, json, time, argparse, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
TOOL = os.path.join(HERE, "h723_stage2_read.py")
OUT = os.path.join(ROOT, "build", "pad_sweep")
DEFAULT_PADS = [0, 4, 8, 12, 16, 20, 24, 28, 64, 256]


def build(n):
    r = subprocess.run(["bash", "build.sh", "-DSCAN_FLASH_PAD=%d" % n],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    return r.returncode == 0, (r.stdout + r.stderr)[-500:]


def flash():
    r = subprocess.run(["pyocd", "flash", "-t", "stm32h723xx",
                        "-O", "connect_mode=under-reset",
                        os.path.join("build", "dcl_h723.hex")],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def measure(n, dur):
    jf = os.path.join(OUT, "pad_%d.json" % n)
    r = subprocess.run([PY, TOOL, "--sweep", "--dur", str(dur), "--json", jf],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(jf):
        return None, (r.stdout + r.stderr)[-400:]
    return json.load(open(jf, encoding="utf-8")), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pads", default=",".join(str(x) for x in DEFAULT_PADS))
    ap.add_argument("--dur", type=float, default=0.5)
    a = ap.parse_args()
    pads = [int(x) for x in a.pads.split(",") if x.strip() != ""]

    os.makedirs(OUT, exist_ok=True)
    print("=" * 92)
    print("落位敏感机制扫描: %d 个填充值 (每个 = 构建+烧录+测量)" % len(pads))
    print("=" * 92)

    recs = []
    for n in pads:
        t0 = time.time()
        ok, log = build(n)
        if not ok:
            print("  PAD=%-4d 构建失败: %s" % (n, log[-200:])); continue
        if not flash():
            print("  PAD=%-4d 烧录失败" % n); continue
        d, err = measure(n, a.dur)
        if d is None:
            print("  PAD=%-4d 测量失败: %s" % (n, err)); continue
        r = d["runs"]
        addr = d["scan_flash_addr"]
        rec = dict(pad=n, addr=addr, mod16=addr % 16, mod32=addr % 32, mod64=addr % 64,
                   size=d["scan_size"],
                   A=r.get("A", {}).get("isr_min", 0),
                   B1=r.get("B1", {}).get("eng_min", 0),
                   B2=r.get("B2", {}).get("eng_min", 0),
                   C1=r.get("C1", {}).get("eng_min", 0),
                   B1_ok=(r.get("B1", {}).get("guard", 0) == 1
                          and r.get("B1", {}).get("tck", 0) == 0x329A37C5),
                   pad_span=(d["pad_end"] - d["pad_start"]))
        recs.append(rec)
        print("  PAD=%-4d addr=0x%08X (%%16=%2d %%32=%2d)  ITCM控制组=%5d  "
              "FLASH·DIRECT=%6d  FLASH·混合=%6d   [%.0fs]"
              % (n, addr, rec["mod16"], rec["mod32"], rec["B2"], rec["B1"], rec["C1"],
                 time.time() - t0))

    if not recs:
        print("没有成功的点"); return 2

    json.dump(recs, open(os.path.join(OUT, "sweep.json"), "w"), indent=1)
    print("\n" + "=" * 92)
    print("汇总 (按引擎地址排序)")
    print("=" * 92)
    print("  %-5s %-12s %5s %5s %8s %10s %10s %-8s" %
          ("PAD", "scan_flash", "%16", "%32", "ITCM对照", "FLASH·DIRECT", "FLASH·混合", "校验"))
    for r in sorted(recs, key=lambda x: x["addr"]):
        print("  %-5d 0x%08X %5d %5d %8d %10d %10d %s"
              % (r["pad"], r["addr"], r["mod16"], r["mod32"], r["B2"], r["B1"], r["C1"],
                 "✓" if r["B1_ok"] else "✗"))

    b1 = [r["B1"] for r in recs]
    b2 = [r["B2"] for r in recs]
    print("\n  FLASH·DIRECT: min=%d max=%d 极差=%d (%.1f%% of min)"
          % (min(b1), max(b1), max(b1) - min(b1), 100.0 * (max(b1) - min(b1)) / min(b1)))
    print("  FLASH·混合  : min=%d max=%d 极差=%d (%.1f%%)  拍长 40000 → %s"
          % (min(r["C1"] for r in recs), max(r["C1"] for r in recs),
             max(r["C1"] for r in recs) - min(r["C1"] for r in recs),
             100.0 * (max(r["C1"] for r in recs) - min(r["C1"] for r in recs))
             / min(r["C1"] for r in recs),
             "至少一个点超载!" if max(r["C1"] for r in recs) > 40000 else "全部拍内"))
    print("  ITCM 控制组 : min=%d max=%d 极差=%d  %s"
          % (min(b2), max(b2), max(b2) - min(b2),
             "✓ 纹丝不动 → 实验有效" if max(b2) - min(b2) <= 8
             else "✗ 居然动了 → 实验无效, 检查是否有别的变量"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
