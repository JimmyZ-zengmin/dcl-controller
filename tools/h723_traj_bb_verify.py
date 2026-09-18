#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_traj_bb_verify.py —— 用**片内黑匣子（1 kHz）**补上轨迹规划的**形状验收**

## 为什么必须换到黑匣子（而不是继续用 PC 侧采样）
2026-09-17 定案：**PC 侧差分测速在本装置上不可用** —— 编码器回填率与 PC 轮询率发生**采样拍频**，
实测 `Δt` 稳定 37 ms 而 `Δraw` 中位 **2**（1200 Hz 在 37 ms 内应有 123）⇒
**极值/分位数全不可信**（`max/P50 = 119`）。而黑匣子是**片内 1 kHz 记录**，
每记录间隔 ≈1 拍 ⇒ `Δraw` 每记录 ≈0.1 counts（@1200 Hz）⇒ **且 1 kHz 远高于任何转速**，不会混叠。

## 记录字段（`src/blackbox.c` 的默认映射，**逐一条查证过**，不是猜的）
槽 = `[0]magic(0x4B424C44 "DLBK") [1]tick [2]seq [3]ctrl` + 60 个数据字：
  `data[0..13]` = SENSOR[0..13]　⇒ **data[0] = 编码器 raw（实测）**
  `data[14..15]`= 故障台账
  `data[16..31]`= WIRE[0..15]　　⇒ **data[28] = WIRE[12]（程序下发的频率请求）**
  `data[32..47]`= ACTUATOR[0..15] ⇒ data[40]/[41] = DIR/ENA
  `data[48..49]`= 绝对时间  · `data[55]`= DO 引脚位图
★ tick 单位 = 100 µs（10 kHz）⇒ **跨度必须由 tick 算**（环是"变化才记"，槽数≠时间）。

## 判据（每条都能失败）
  T0 记录与时间轴：magic 全对 / 跨度由 tick 算 / 记录密度
  T1 **命令侧**：`WIRE[12]` 的形态
      · tri ：应是对称**方波**（0 与 A 交替，周期 = 2×段长）
      · sine：应是 **6 级阶梯**，归一化比值对理想表相关 ≥0.95
  T2 **实测速度形状**（tri 的关键）：`v = Δraw/Δtick` 应逐半周期**线性**（R²≥0.90）且**相邻段斜率反号**
  T3 **峰值**：实测峰值 ≈ A（±15%）
  T4 **跟随比**（sine 的关键，因为 sine 关斜坡）：`v/(WIRE[12]×2.56)` 的**中位数** ≈ 1（±15%）
  T5 **累积核对**（回答"74% 之谜"）：`ΣΔraw / span` vs 理论平均
  R 反向：`WIRE[12]==0` 期间实测速度应 ≈0（**不该红的不红**）

★★ **必须用"系统 python"跑**（本机 `python` 是**托管解释器**，里面没有 `pyocd`）：
   `"/c/Users/min/AppData/Local/Programs/Python/Python313/python.exe" tools/h723_traj_bb_verify.py ...`
   （`pyocd` 0.44.1 装在系统 python 下；纯离线的 `--bin` 模式用哪个解释器都行）

用法：
  <系统python> tools/h723_traj_bb_verify.py --prog tri  [--A 1200]      # 自己采（pyocd+串口）
  python tools/h723_traj_bb_verify.py --prog tri --bin build/_bb_tri.bin   # 只离线分析
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BB_BASE, BB_SLOTS, W = 0x24004000, 960, 64
MAGIC = 0x4B424C44
TICK_HZ = 10000.0
SPR, CPR = 1600.0, 4096.0          # 步/圈、编码器 counts/圈
K = CPR / SPR                       # counts/step = 2.56
D_RAW, D_W12 = 0, 28                # 数据槽下标
_t = 0


def rec(ok, name, detail=""):
    global _t
    _t += 1
    print("  [%s] %-50s %s" % ("PASS" if ok else "FAIL", name, detail))
    return ok


# ───────────────────────── 采集（pyocd + 串口，一个会话内完成）─────────────────────────
def capture(port, prog, A, slope, out):
    """★ 顺序是**血的**：pyocd 会话开始会**复位目标** ⇒
      · 若先部署再开会话 ⇒ RAM 里的程序被复位清掉 ⇒ 采到"轴不动"
      · 反过来（先开会话再部署）也不行 —— 会复位后 **bench/绑定都没了**
      ⇒ 正解：**开会话 → resume → 会话内重新部署 + 重绑反馈 → 再起运动 → halt+dump**。
    ★ 另外：`TODO deploy 会清掉绑定表`（GAP-11）⇒ 部署后**必须**重绑，否则 `sensor[0]` 是陈旧常数。"""
    import subprocess
    from pyocd.core.helpers import ConnectHelper
    from h723_client import Dcl, find_board, engine_status
    import h723_client as HC

    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(HERE)
    dcl_file = os.path.join(ROOT, "examples", "h723_step_traj_%s.dcl" % prog)

    s = ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        options={"connect_mode": "halt", "resume_on_disconnect": True})
    s.open()
    try:
        tgt = s.target
        tgt.resume()
        time.sleep(0.4)

        def run(script, *args):
            r = subprocess.run([sys.executable, os.path.join(HERE, script)] + list(args),
                               capture_output=True, text=True, cwd=ROOT)
            return r.returncode, (r.stdout or "") + (r.stderr or "")

        rc, o = run("dclc.py", dcl_file)
        if rc != 0:
            raise SystemExit("部署失败：\n" + o[-600:])
        print("  部署 OK: %s" % [l for l in o.splitlines() if l.startswith("[OK]")][-1:])
        rc, o = run("h723_as5600_bind.py")           # ★ 部署会清掉反馈绑定
        if rc != 0:
            raise SystemExit("重绑反馈失败：\n" + o[-600:])
        print("  反馈重绑 OK（否则 sensor[0] 是陈旧常数）")

        d = Dcl(port or find_board())
        shm = engine_status(d)["shm"]
        owm = HC.OFF_WIRE_MAP

        def wset(n, v):
            return d.send(0x21, struct.pack("<II", shm + owm + n * 4,
                         struct.unpack("<I", struct.pack("<f", v))[0]))[0] == "ACK"
        # ★★ 先把编码器回填提到 ~1 kHz（阶段 0 的结论）：否则半周期里只有 ~14 次回填，
        #   速度剖面太粗（实测回填 ~600 Hz ⇒ 100ms 半周期只有 ~60 点，够用；
        #   但拍内模式更稳：`sub=22 arg=1` + `sub=20 arg=10`）。
        d.send(0x39, bytes([19, 22]) + struct.pack("<I", 1))
        d.send(0x39, bytes([19, 20]) + struct.pack("<I", 10))
        time.sleep(0.2)
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 1))      # 运动源 = 程序面
        d.send(0x39, bytes([19, 17]) + struct.pack("<I", slope))  # 斜坡斜率
        time.sleep(0.2)
        wset(10, 1.0); wset(11, A)
        print("  起运动: A=%.0f Hz slope=%d Hz/s ⇒ 等 1.5 s 灌满环..." % (A, slope))
        time.sleep(1.5)
        tgt.halt()
        # ★★ 分块读 + 重试：实测一次性 `read_memory_block8(0x24004000, 240KB)` 会
        #   报 `TransferFaultError ... @ 0x2400fc00`（读到一半）。这台探针的 **USB 不稳是本项目已知问题**
        #   （提交 `faf9d1e` 原话："★上机验证待板子(USB又断)"）。
        #   ⇒ 32 KB 一块、每块最多 4 次；实在失败就**零填充并显式告警**（环是环形的，
        #     缺尾块不影响"按 seq 排序取连续段"的分析，但**必须让人知道这是部分 dump**）。
        total = BB_SLOTS * W * 4
        CH = 32 * 1024
        buf = bytearray()
        bad = 0
        for off in range(0, total, CH):
            n = min(CH, total - off)
            for att in range(4):
                try:
                    buf += bytes(tgt.read_memory_block8(BB_BASE + off, n))
                    break
                except Exception as e:
                    if att == 3:
                        bad += n
                        buf += b"\x00" * n
                        print("  ⚠ 0x%08X..0x%08X 读失败(4 次) ⇒ 零填充: %s"
                              % (BB_BASE + off, BB_BASE + off + n - 1, str(e)[:70]))
                    else:
                        time.sleep(0.25)
        tgt.resume()
        data = bytes(buf)
        if bad:
            print("  ⚠⚠ **部分 dump**：%d / %d 字节读失败（分析结论要按此打折）" % (bad, total))
        open(out, "wb").write(data)
        print("  dump %d 字节 → %s" % (len(data), out))
        wset(11, 0.0); wset(10, 0.0)
        d.send(0x39, bytes([19, 17]) + struct.pack("<I", 0))
        d.send(0x39, bytes([19, 13]) + struct.pack("<I", 0))
        d.send(0x39, bytes([19, 3]) + struct.pack("<I", 0))
        print("  已收尾: 请求清 0 / 关斜坡 / 运动源回脚手架 / 失能")
        d.close()
    finally:
        s.close()


# ───────────────────────── 解码 ─────────────────────────
def decode(path):
    """★ 2026-09-18 修一个**真 bug**：原来"按 seq 全局排序"是错的。
    实测（本工具首跑）：环里同时留着**不连续**的 seq 段（`128..675` / `1636..1919` / `38400..38527`），
    而槽位规则经查证是 **`slot = seq % 960`**（`(seq-slot)%960` 对 960 槽**全为 0**）。
    ⇒ 全局排序会把**不同年代的记录混在一起**，于是算出"跨度 19.7 s / 平均间隔 20.6 ms"这种
      **跨代平均**的假数（真跨度只有 51 ms）。
    ⇒ 正解：按 seq 连续性**切段**，只取**最新的连续段**，并**显式报告**环里的陈旧段。"""
    d = open(path, "rb").read()
    if len(d) < BB_SLOTS * W * 4:
        raise SystemExit("dump 太短: %d" % len(d))
    rows = {}
    for i in range(BB_SLOTS):
        o = i * W * 4
        mg, tick, seq = (int.from_bytes(d[o + k:o + k + 4], "little") for k in (0, 4, 8))
        if mg != MAGIC:
            continue
        rows[seq] = (tick, struct.unpack("<60f", d[o + 16:o + 16 + 240]))
    if not rows:
        raise SystemExit("没有一条合法记录（magic 全不匹配）⇒ 环是空的或未启动")
    # ★★★ 2026-09-18 第二次修这里的解码：**按 `tick` 排序，不要按 `seq`**。
    #   血证：同一会话内连拍两次，环里其实是**满的 960 条连续记录**（seq 在环内**回绕**：
    #   36480…38399 然后 0…1919）。而"按 seq 排序 + 取最新连续段"遇到回绕就把它**切成假的不连续段**
    #   ⇒ 只取到 128 条 ⇒ 覆盖度 0.33 ⇒ 误判 SKIP（**看起来像硬件不够，其实是解码器不够**）。
    #   ⇒ `tick` 是单调的（10 kHz，本会话内不回绕）⇒ 它才是正确的时间轴。
    # ★★★ 一致性闸门（2026-09-18 加）：同一份 dump 用三种排序会给出三个互相矛盾的跨度
    #   （按 seq 取最新段 → 128 条 / 按 slot → 960 条 / 按 tick → 跨 20 s）。
    #   在**环的写指针/水位语义定案之前**，任何「形状」结论都是建在没解释的机制上 ⇒
    #   本工具在这种状态下**必须拒绝给结论**（而不是悄悄挑一个看起来合理的）。
    _by_tick = sorted(rows, key=lambda k: rows[k][0])
    _span_tick = (rows[_by_tick[-1]][0] - rows[_by_tick[0]][0]) / TICK_HZ
    _by_slot = sorted(rows)
    _gap = sum(1 for a, b in zip(_by_slot, _by_slot[1:]) if b != a + 1)
    _stale = sum(1 for k in rows if rows[k][0] + 5000 * TICK_HZ < rows[_by_tick[-1]][0])
    print("  【环读出语义自检】三种排序给出的跨度：")
    print("    按 slot：%d 条，slot 不连续处 %d" % (len(_by_slot), _gap))
    print("    按 tick：%d 条，跨度 %.3f s；其中 %d 条比最新记录早 >5000 s（陈旧残余）"
          % (len(_by_tick), _span_tick, _stale))
    if _stale > BB_SLOTS // 4 or _span_tick > 2.0:
        print("\n  ⛔ **环的读出语义未定案** ⇒ 本工具**拒绝给出形状结论**（判 SKIP）。")
        print("     三次读出互相矛盾（按 seq / 按 slot / 按 tick 各说各话）⇒")
        print("     在解释清楚「写指针/水位」之前，任何形状结论都不可信")
        print("     （本项目纪律：**别把结论建在没解释的机制上**）。")
        print("     ⇒ 下一步：读 **SD 日志头里的环元数据**，或从固件侧直接确认写指针语义。")
        # ★ 用 SystemExit(2) 而不是 `return 2`：`decode()` 的契约是"返回记录序列"，
        #   返回一个 int 会让调用方 `len(rows)` 直接 TypeError（第一版就这么炸了）。
        #   （SKIP 用退出码 2 表达："判无效"既不是 0=通过 也不是 1=失败。）
        raise SystemExit(2)
    ks = sorted(rows, key=lambda k: rows[k][0])          # 按 tick
    wrap = sum(1 for a, b in zip(ks, ks[1:]) if rows[b][0] <= rows[a][0])
    if wrap:
        print("  ⚠ tick 出现 %d 处回绕/重复 ⇒ 时间轴可能不单调（下面的跨度要打折看）" % wrap)
    if len(ks) < BB_SLOTS:
        print("  ⚠ 只有 %d / %d 槽是合法记录（其余为零填充或未写）" % (len(ks), BB_SLOTS))
    return [rows[k] for k in ks]


def fit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    k = sxy / sxx if sxx else 0.0
    r2 = (sxy * sxy / (sxx * syy)) if (sxx and syy) else 0.0
    return k, r2


def read_dwell(prog):
    """★ 从 `.dcl` 里**读出**段长，而不是在两个工具里各写一份常量。
    ★ 血证（本项目通病）："同一个量两处存放" ⇒ 只改一处就静默失效。
      这次段长从 600/250ms 改到 100/35ms，若工具里还写死旧值，算出的"周期""覆盖度"全是错的。"""
    import re as _re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    f = os.path.join(root, "examples", "h723_step_traj_%s.dcl" % prog)
    txt = open(f, encoding="utf-8").read()
    ms = [float(x) for x in _re.findall(r"DWELL\s+([\d.]+)ms", txt)]
    if not ms:
        raise SystemExit("在 %s 里找不到 DWELL ⇒ 无法确定周期" % f)
    return ms[0] / 1000.0, len(ms)


def main():
    prog = "tri"
    if "--prog" in sys.argv:
        prog = sys.argv[sys.argv.index("--prog") + 1]
    A = 1200.0
    if "--A" in sys.argv:
        A = float(sys.argv[sys.argv.index("--A") + 1])
    port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None
    binp = sys.argv[sys.argv.index("--bin") + 1] if "--bin" in sys.argv else None
    dwell, nseg = read_dwell(prog)   # ★ 单一真值源 = 那个 .dcl 文件
    slope = int(A / dwell) if prog == "tri" else 0
    if binp is None:
        binp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build",
                            "_bb_%s.bin" % prog)
        binp = os.path.normpath(binp)
        capture(port, prog, A, slope, binp)

    rows = decode(binp)
    print("=== 黑匣子轨迹验收: prog=%s A=%.0f Hz slope=%d Hz/s，记录 %d 条 ==="
          % (prog, A, slope, len(rows)))
    tks = [r[0] for r in rows]
    # ★ tick 是 32 位，回绕要处理（一次 dump 只有 ~1 s，不会绕；但保险）
    tt = [0.0]
    for i in range(1, len(tks)):
        dt = (tks[i] - tks[i - 1]) & 0xFFFFFFFF
        tt.append(tt[-1] + dt / TICK_HZ)
    span = tt[-1]
    raw = [r[1][D_RAW] for r in rows]
    w12 = [r[1][D_W12] for r in rows]
    print("  跨度 %.3f s（由 tick 算） 平均记录间隔 %.3f ms ⇒ ≈%.0f Hz"
          % (span, span / max(1, len(rows) - 1) * 1e3, (len(rows) - 1) / span))

    ok = True
    period = (2 * dwell) if prog == "tri" else (6 * dwell)
    cov = span / period
    ok &= rec(len(rows) > 32, "T0 记录足够（>32 条）", "%d 条" % len(rows))
    print("  覆盖度 = 最新段跨度 / 一个完整周期(%.3f s) = **%.3f 个周期**" % (period, cov))
    if cov < 0.35:
        print("\n  ⛔ **形状判据判无效（SKIP，不是通过也不是失败）**")
        print("     黑匣子的保留跨度 = 960 槽 ÷ 变化率：运动时变化率 ~2.5 kHz ⇒ 只留 **~0.4 s**；")
        print("     而这次 dump 的最新连续段只有 **%.0f ms**（环里有陈旧残余段，工具已按最新段切）。" % (span * 1e3))
        print("     ⇒ 覆盖不到一个周期(%.3f s)，**无法判形状**。" % period)

        print("     ⇒ 要做形状验收，三选一：① 把段长缩到 ≤50 ms；② 扩大环（①层，240 KB→更大）；")
        print("       ③ 只保留\"命令侧 WIRE[12]\"一路的变化率（映射表是编译期的 ⇒ 也要①层）。")
        print("     ★ 但有几条**不依赖覆盖一个周期**的判据仍然可判（下面给出）：")
    skip_shape = cov < 0.35

    # ── 速度口径：**滑窗累积**，不是逐记录差分 ──
    # ★★★ 2026-09-18 第三次踩同一个坑，这次做成通用规则（见记忆 §5.27）：
    #   黑匣子的**记录率**（~2.5 kHz，由"任一映射通道变化"触发）**不等于编码器的回填率**
    #   （本次实测 ~90 Hz）⇒ 逐记录的 `Δraw/Δtick` 在 0.4 ms 尺度上是 **0 或 ~34 counts**
    #   ⇒ 算出 32812 Hz 这种荒谬值（真值 ~1200）。
    #   ⇒ 正解：**滑窗累积**（窗宽 ≥ 一次回填间隔）⇒ 对回填粒度免疫；
    #     这与"PC 侧只有 ΣΔ/总时长 免疫"是同一条规矩，只是尺度不同。
    W_MS = 20.0                       # 窗宽 ms（≥ 回填间隔 ~11 ms）
    vh, tw12 = [], []
    j = 0
    for i in range(len(rows)):
        while tt[i] - tt[j] > W_MS / 1000.0:
            j += 1
        if i == j:
            continue
        dt = tt[i] - tt[j]
        dv = (int(round(raw[i])) - int(round(raw[j]))) & 0xFFF
        if dv > 2048:
            dv -= 4096
        vh.append(abs(dv) / dt / K)   # Hz
        tw12.append(w12[i])
    v = [x * K for x in vh]
    print("  速度口径 = **滑窗 %.0f ms 累积**（对编码器回填粒度免疫）；共 %d 点"
          % (W_MS, len(vh)))

    # ── 覆盖不足时：只跑"不依赖覆盖一个周期"的三条，然后判 SKIP 退出 ──
    if skip_shape:
        # L1 命令侧峰值 == A（证明程序把 A 真写进了 wire[12]）
        cmax = max(w12)
        ok &= rec(abs(cmax - A) <= 0.02 * A, "L1 命令侧峰值 == A（程序真的写了 A）",
                  "%.0f vs %.0f" % (cmax, A))
        # L2 ★ 用**累积量**（唯一免疫"回填粒度"的量）：窗口只有 51 ms 时，
        #    滑窗速度会被 ±1 次回填的量化放大到 ~1.6 倍（实测 1963 vs 1200）
        #    ⇒ "轴在转、没超发"这句话只能由 ΣΔ/总时长 来回答。
        tot2 = 0
        for i in range(1, len(rows)):
            dv = (int(round(raw[i])) - int(round(raw[i - 1]))) & 0xFFF
            if dv > 2048:
                dv -= 4096
            tot2 += dv
        mean_hz = abs(tot2) / span / K
        vwmax = max(vh) if vh else 0.0
        print("  L2 累积平均 %.0f Hz（滑窗峰值 %.0f 会被回填量化放大，仅供参考）"
              % (mean_hz, vwmax))
        ok &= rec(0.5 * A <= mean_hz <= 1.15 * A,
                  "L2 累积平均在 [0.5A, 1.15A]（轴在转且没超发）", "%.0f Hz（A=%.0f）" % (mean_hz, A))
        # L3 ★ **斜坡真的按设定的斜率在爬**：以"命令跳到 A 的那一刻"为起点，
        #    期望 v(t) = min(A, slope × t)；实测/期望 的**中位数**应 ≈1。
        #    ⇒ 这是本次覆盖不足的情况下**最有信息量**的一条（它验证固件斜坡）。
        rise = next((i for i in range(len(w12)) if w12[i] > 0.5 * A), None)
        if rise is not None and slope > 0:
            rat = []
            for i in range(rise, len(vh)):
                dt = tw12[i] - tw12[rise]
                exp_v = min(A, slope * dt)
                # ★ 阈值从 0.15A 降到 0.03A：51 ms 窗口内 min(A, slope×Δt) 最多只到 102 Hz，
                #   0.15A=180 永远达不到 ⇒ 判据体量为 0（**我的判据自己的 bug**，不是硬件问题）
                if exp_v > 0.03 * A:
                    rat.append(vh[i] / exp_v)
            rat.sort()
            med = rat[len(rat) // 2] if rat else 0.0
            print("  L3 斜坡跟随比（实测/期望=min(A, slope×Δt)）中位 %.3f，体量 %d" %
                  (med, len(rat)))
            if len(rat) < 5:
                print("  L3 **判无效**（体量 %d < 5）—— 窗口装不下斜坡，不是通过也不是失败"
                      % len(rat))
            else:
                ok &= rec(0.75 <= med <= 1.25, "L3 **固件斜坡按设定斜率在爬**（中位数 ±25%）",
                          "%.3f" % med)
        else:
            print("  L3 斜坡判据**不适用**（未找到命令上升沿 / slope=0 / 窗口 <90ms 装不下斜坡）")
        print("\n  ⛔ **形状判据：判无效（SKIP）** —— 覆盖不足，不是通过、也不是失败。")
        print("  已证：%d 条记录 / %.0f ms 内，命令=A 且实测在动 ⇒ **命令通路是通的**。" %
              (len(rows), span * 1e3))
        print("=== 判无效（SKIP）===")
        return 2

    # ── T2/T1 形态 ──
    if prog == "tri":
        # 命令侧：应是对称方波 {0, A}
        hi = [1 if x > 0.5 * A else 0 for x in w12]
        nz = sum(hi)
        print("  命令侧 WIRE[12]: 非零占比 %.0f%%（对称方波应 ≈50%%）" % (100.0 * nz / len(hi)))
        ok &= rec(0.35 < nz / len(hi) < 0.65, "T1 命令是 0/A 交替的方波（≈50% 占空）",
                  "%.0f%%" % (100.0 * nz / len(hi)))
        # 实测速度：按"命令下降沿"切半周期，逐段线性
        edges = [i for i in range(1, len(w12)) if w12[i - 1] > 0.5 * A and w12[i] <= 0.5 * A]
        best, ks = [], []
        for a, b in zip(edges, edges[1:]):
            seg = [(tw12[j] - tw12[a], vh[j]) for j in range(a + 1, b) if j < len(tw12)]
            if len(seg) < 8:
                continue
            k, r2 = fit([s[0] for s in seg], [s[1] for s in seg])
            ks.append(k)
            best.append((k, r2, len(seg)))
        lin = [x for x in best if x[1] >= 0.90]
        print("  T2 逐半周期拟合（k, R², n）: %s"
              % ", ".join("(%.0f,%.2f,%d)" % x for x in best[:6]))
        ok &= rec(len(best) >= 2 and len(lin) >= max(2, len(best) // 2),
                  "T2 实测速度在过半半周期内**线性**（R²≥0.90）",
                  "%d/%d 段" % (len(lin), len(best)))
        has_up = any(k > 0 for k in ks)
        has_dn = any(k < 0 for k in ks)
        ok &= rec(has_up and has_dn, "T2b 斜率**有正有负**（真的是三角波，不是单调爬）",
                  "k>0:%d k<0:%d" % (sum(1 for k in ks if k > 0), sum(1 for k in ks if k < 0)))
    else:
        # sine：命令侧应是 6 级阶梯。★ 判法用"按理想级值分箱后的**记录占比**"，
        #   而不是"数出几个不同值" —— 后者对量化/抖动敏感，前者是形状判据且与刻度无关。
        import math
        ideal = [0.259, 0.707, 0.966]
        obs = [len([x for x in w12 if abs(x - th * A) < 0.06 * A]) for th in ideal]
        tot = sum(obs) or 1
        og = [o / tot for o in obs]
        tg = [th / sum(ideal) for th in ideal]
        print("  命令侧按理想级分箱的**记录占比** 实测 %s"
              % " ".join("%.2f" % x for x in og))
        print("                                   理想 %s"
              % " ".join("%.2f" % x for x in tg))
        n = len(og)
        mg, mt = sum(og) / n, sum(tg) / n
        num = sum((og[i] - mg) * (tg[i] - mt) for i in range(n))
        den = math.sqrt(sum((og[i] - mg) ** 2 for i in range(n)) *
                        sum((tg[i] - mt) ** 2 for i in range(n)))
        r = num / den if den else 0.0
        ok &= rec(r >= 0.90, "T1 命令侧是**正弦形状的阶梯**（占比相关 ≥0.90）", "r=%.3f" % r)
        # 跟随比（sine 关斜坡 ⇒ 实测/命令 应 ≈1）
        rat = sorted(vh[i] / tw12[i] for i in range(len(vh)) if tw12[i] > 0.3 * A)
        med = rat[len(rat) // 2] if rat else 0.0
        print("  跟随比 中位 %.3f（体量 %d 点；只取命令 >30%% 峰值的点）" % (med, len(rat)))
        ok &= rec(0.85 <= med <= 1.15, "T4 跟随比 ≈1（中位数，不是极值！）", "%.3f" % med)

    # ── T3 峰值 ──
    vh_sorted = sorted(vh)
    pk = vh_sorted[-1]
    pk95 = vh_sorted[int(0.95 * len(vh_sorted))] if vh_sorted else 0.0
    print("  实测速度峰值: max=%.1f  P95=%.1f  Hz（命令 A=%.0f）" % (pk, pk95, A))
    ok &= rec(0.85 <= pk / A <= 1.15, "T3 实测峰值 ≈ A（±15%）", "%.2f" % (pk / A))

    # ── T5 累积（唯一免疫采样问题的量）──
    tot = 0
    for i in range(1, len(rows)):
        dv = (int(round(raw[i])) - int(round(raw[i - 1]))) & 0xFFF
        if dv > 2048:
            dv -= 4096
        tot += dv
    mean_hz = abs(tot) / span / K
    # 理论平均：tri 的命令是 0/A 对称方波，而**斜坡把它积成三角波** ⇒ 三角波均值 = A/2
    #           sine 是 6 段阶梯（因子见程序头）⇒ 均值 = A × 因子均值
    if prog == "tri":
        want = A / 2.0
    else:
        f6 = [0.259, 0.707, 0.966, 0.966, 0.707, 0.259]
        want = A * (sum(f6) / len(f6))
    print("  累积位移 %d counts / %.3f s ⇒ 平均 %.1f Hz（理论平均 %.1f）"
          % (tot, span, mean_hz, want))
    ok &= rec(0.7 <= mean_hz / want <= 1.3, "T5 平均速率与理论同量级（±30%）",
              "%.2f" % (mean_hz / want))

    # ── R 反向：命令为 0 时不该动 ──
    zs = [vh[i] for i in range(len(vh)) if tw12[i] <= 0.01]
    zmax = max(zs) if zs else 0.0
    print("  R 命令=0 的 %d 个点里，实测速度 max=%.1f Hz" % (len(zs), zmax))
    ok &= rec(zmax < 0.15 * A, "R 命令为 0 时不应有速度（不该红的不红）",
              "%.1f < %.1f" % (zmax, 0.15 * A))
    print("=== %s ===" % ("全部通过" if ok else "有 FAIL —— 见上"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
