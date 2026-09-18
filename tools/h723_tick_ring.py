#!/usr/bin/env python3
"""
h723_tick_ring.py — **逐拍**预测 vs 逐拍实测（E4 的严格形态）

★★ 与"累积量"的区别
  `MIN/MAX/SUM` 能回答"典型拍稳不稳"（实测 0.02~0.06 cyc），但**回答不了"
  哪一拍贵、贵多少"**。E4 要判的是**逐拍**对齐 ⇒ 需要每拍一条、不丢拍的记录。
  载体: SHM 环形缓冲 `OFF_EXEC_RING`（`engine.h`），**ISR 直接写**、无镜像滞后。

★★ 判据为什么先做"纯结构性"的那一条
  成本模型（Σc + k×转变数 + c₀）只在 2 个原语对上验过。而**桶调度的结构**是确定的:
      nrun(t) = n_div0 + cnt1[t%10] + cnt2[t%100]
  ⇒ 实测 `di` 的**不同取值**应与**不同的 nrun** 一一对应, 且**每个取值的出现频次**
    应等于 `t ∈ [0,100)` 中该 nrun 出现的次数。
  这条判据**完全不依赖成本模型** ⇒ 它能在"模型还不全"的时候就判定
  "引擎确实按桶表在跑, 而且每拍的成本只由本拍跑的路由集合决定"。

判据（每条都能失败）
  P0a 前置: 板子健康（`tick` 在百万级 —— 两位数 = 复位循环 ⇒ 判无效）
  P0b 前置: 环写计数 ≈ RUN 拍数（窗口是连续的, 没被大读吃掉）
  ★ 为什么必须有这两条前置: 本工具曾**每次**读到 `tick≈70`（板子在复位环里）, 而
    当时的判据把那个窗口硬算下去, 得出的直方图"看着也像数据"。**SKIP ≠ PASS**。

  P1 ★ **模型预言均值 vs 实测窗口均值**（本工具现在唯一的主判据）
     模型: `E[di] = c0 + c_route × E[nrun]`, 其中 `E[nrun]` 由**实读的桶表**算出
     （`nrun(t) = n_div0 + cnt1[t%10] + cnt2[t%100]`），`c0`/`c_route` 由 E1 两点法
     **独立标定**、经 `--c0/--c-route` 传入 —— **不是**从本窗口拟合。
     判据: |实测均值 − 预言| ≤ `--sigma` × 标准误（默认 3）。

  ✗ 已删除的旧判据 P1/P2/P3（"实测取值个数/频次 == 预测 nrun 的取值个数/频次"）:
     它们隐含"`di` 是 `nrun` 的**确定性函数**"这一从未声明的假设。实测（256 拍健康窗口）
     给出: 结构预测 2 个档位（12 条占 20/100 拍、13 条占 80/100 拍），而 `di` 有 **13 个**
     取值 —— 因为 `di` 是**整段 ISR** 的时长, 还含黑匣子快照（每 1024 拍一次重活）、
     ADC 启动、RTC 锁存、I2C 状态机与逐拍的数据相关分支。
     ⇒ 那三个判据的 FAIL 是**正确**的: 它们推翻的是一个真假设。留在这里是因为
       "被数据推翻的判据"本身是资产（见 `RETRACTIONS.md`）。

用法
  python tools/h723_tick_ring.py --prog 5,1,128     # 128×PID div1
  python tools/h723_tick_ring.py --prog 5,2,128     # 128×PID div2（100 拍长周期）
退出码: 0 = 全过 / 1 = 有 FAIL / 2 = 前置不满足（判无效）
"""
import sys as _sys_enc
try:
    _sys_enc.stdout.reconfigure(errors="replace")
    _sys_enc.stderr.reconfigure(errors="replace")
except Exception:
    pass

import argparse, os, struct, sys, time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from h723_client import Dcl  # noqa: E402

cmd_status, cmd_deploy, cmd_stop, cmd_start, cmd_burst = 0x38, 0x10, 0x12, 0x11, 0x22
OFF_ROUTE_TABLE, OFF_ROUTE_BUCKETS = 0x0840, 0x4480
OFF_EXEC_RING_HDR, OFF_EXEC_RING = 0x3880, 0x3890
RING_SLOTS = 256
B1P, B2P = 10, 100
ACTIVE = 0x01
SRC_CONST, DST_WIRE = 2, 2
# ★ `tick` 健康阈值与判读（2026-09-18 实测标定, 依据见 `_wait_healthy`）
TICK_HEALTHY = 200000      # 干净启动后 ~10 s 就到这里（实测 8 s 后 4.03e6）
HEALTH_TRIES = 6
SHM_SEEN = 0x20004E80      # 见 `main()` 里"硬编码是刻意的"说明


def mk(op, div, n):
    """构造 0x10 DEPLOY 载荷。

    ★★ 2026-09-18: 加**入参闸门**。原来 `n>255` 会让 `struct.pack("<B", ...)` 抛
       `struct.error` —— 工具**在发帧之前就崩了**, 而崩的位置在 `mk()` 里, 症状是
       "n=384/512 什么都没打印、退出码 1"。现场看到的是"板子没反应", 真实原因是
       **上位机自己崩了**（本项目既有教训: 把上位机的失败误归因到板子）。
    ★ 而且 `u8` 这个上限是**协议事实**: `n_routes` 在载荷头里是 1 字节
      ⇒ 与固件的 `MAX_ROUTES=128` 合起来, 路由数**结构上**不可能超过 255。
      ⇒ 所以 "div2 的稀疏区（每拍 <1.28 条）" 是**测不到的**, 不是"还没测"。
    """
    if not (1 <= n <= 128):
        raise SystemExit("!! n=%d 超出范围 1..128（固件 MAX_ROUTES=128; "
                         "载荷头 n_routes 是 u8, >255 会直接崩在打包处）" % n)
    if not (0 <= div <= 2):
        raise SystemExit("!! div=%d 超出范围 0..2" % div)
    # ★★ 2026-09-18: 必须给路由带上**第二输入**（`ROUTE_FLAG_WIRE2` + `wire2_idx`）。
    #   否则固件的 deploy 校验会 NAK:
    #       "this op needs wire2 source (set ROUTE_FLAG_WIRE2 or wire2_idx!=0)"
    #   实测: CNT(0x0B) / ARITH(0x0D) / AND(0x0F) / OR(0x10) / SR(0x12) 这 5 个原语
    #   用不到第二输入就无法部署 ⇒ E-C 的 19 个原语里**缺了 5 个**。
    #   ★ 判据见 `engine.h:1038` 的 `wire2_valid`: `(flags & WIRE2) || wire2_idx`。
    #   ★ 教训: 这是**第三类**同类问题 —— 前两类是 `mk()` 崩在打包(§入参闸门)与
    #     "payload 字段错位"。三次都表现为"板子没反应/工具报错", 而根因都在上位机。
    FLAGS = ACTIVE | 0x02                      # 0x01 ACTIVE | 0x02 ROUTE_FLAG_WIRE2
    routes = b"".join(struct.pack("<BBBBBBHHHHBB", SRC_CONST, i, DST_WIRE, i, op,
                                  FLAGS, i, (i % 64) + 1, 0, i, div, 0) for i in range(n))
    #                                                 ↑wire2_idx=i（非零 ⇒ wire2_valid 成立）
    params = b"".join(struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) for _ in range(n))
    states = b"\x00" * (16 * (min(n, 64) + 1))
    return struct.pack("<HHH", n, n, min(n, 64) + 1) + routes + params + states + b"\x00" * 16


def rd(dcl, addr, nwords, chunk=200):
    out, off = b"", 0
    while off < nwords:
        k = min(chunk, nwords - off)
        sts, p = dcl.send(cmd_burst, struct.pack("<IH", addr + 4 * off, k), expect_len=None)
        if sts != "ACK" or len(p) < 4 * k:
            return None
        out += p[:4 * k]
        off += k
    return out


def _tick_of(dcl, addr=SHM_SEEN):
    """读环头 → (环写, tick)；失败返回 None。★ 只读 **2 个字**：
    大读会阻塞主循环（实测 ~60 ms/128 字）, 而这里只需要两个计数器。"""
    q = rd(dcl, addr + OFF_EXEC_RING_HDR, 2)
    if q is None or len(q) < 8:
        return None
    return struct.unpack("<2I", q[:8])


def _wait_healthy(dcl, shm, tries=HEALTH_TRIES, need=3):
    """★★★ 2026-09-18: **等板子真正跑起来再测** —— 这一条是 E4 卡住的真正解药。

    ## 现场证据（不是推断）
    工具在**发出任何一帧之前**的第一条自证探针就读到 `tick=78`；而在它前一次运行
    的同一块板上, 同一地址读到的是 `tick=4.28e6`。⇒ 板子在**打开串口那一刻**
    就已经处于"复位—恢复态", 与本工具的收发序列**无关**。

    ## ★★ 为什么参数从"硬编码地址"改成"传入 shm"
    第一版用 `SHM_SEEN = 0x20004E80`（硬编码）—— 理由是"不发 0x38 也能读",
    这样自证探针可以放在**任何帧之前**。
    **但 2026-09-18 加 E-D 的 SCAN 域之后 `g_shm` 移到了 `0x20004EA0`**,
    硬编码地址立刻**失效**, 而症状是"板子不健康 ⇒ 判无效" ——
    **板上明明一切正常**（`samples` 在涨、`run=1`）。
    ⇒ 这是一个**静默失效**: 板子换了, 工具不知道, 还给出一个看起来像结论的判断。
    ⇒ 修法: 地址一律**问 0x38 要**（那正是 0x38 的用途）。
      代价是自证探针不能放在 0x38 之前 —— 但"板子在发帧前是否已坏"这个问题
      **已经查清并写进文档**, 探针的历史使命已完成, 不必再用硬编码换它。

    ## 判据与为什么它能失败
    `tick` 是 ISR 里裸自增的自由计数（`main.c` 的 `g_tick_count++`），
    **不随 `stats_reset` 归零**（实测: STOP/START 后 `环写` 766126→1567 而 `tick` 继续走）。
    ⇒ `tick` 就是"本次上电活了多久"的探针。
    ⇒ **要求连续 `need` 次读到 ≥ `TICK_HEALTHY`** 才能开工:
      复位循环**不可能**满足, 干净启动必然满足。
    """
    for i in range(tries):
        t0 = time.time()
        good = 0
        while time.time() - t0 < 12.0:
            v = _tick_of(dcl, shm)
            if v is None:
                good = 0
            elif v[1] >= TICK_HEALTHY:
                good += 1
                if good >= need:
                    print("    [健康门] 第 %d 次尝试: tick=%d 环写=%d ⇒ 开工（等了 %.1f s）"
                          % (i + 1, v[1], v[0], time.time() - t0))
                    return True
            else:
                good = 0
            time.sleep(0.25)
        print("    [健康门] 第 %d 次尝试: 12 s 内 tick 始终 < %d ⇒ 强制复位重来"
              % (i + 1, TICK_HEALTHY))
        try:
            dcl.close()
        except Exception:
            pass
        time.sleep(0.4)
        dcl.__init__(dcl.port, wait=1.0)
    print("    [健康门] %d 次仍未健康 ⇒ **判无效**（不测一个正在复位的板子）" % tries)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--prog", required=True, metavar="OP,DIV,N")
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--json", default=None)
    # ★ 线性成本模型的**独立标定常数**。
    #   ★★ 单位（2026-09-18 踩过）: `di` 是 **TB tick**, 而下面两个常数是 **CPU cycle**。
    #     `TB_HZ`(TIM5)=200 MHz 而 CPU=400 MHz ⇒ 差 **2 倍**。不换算就会出现
    #     "模型 2938 / 实测 1376" 这种**看似结论、实则单位错**的偏差。
    #   ★★ 标定来源（2026-09-18, `.tmpctl/e4_calib.py`，div1, n∈{16,32,64,128}）:
    #     用 **n=16 与 n=128 两点**定标 ⇒ `c_route=121.2, c0=1200`,
    #     再用**没参与拟合**的 n=32 / n=64 做预测检验:
    #         偏差 **+1.6 cyc**(±11.4) 与 **−0.6 cyc**(±13.2)  ⇒ 线性模型外推成立。
    #     三段斜率 122.3 / 120.6 / 121.3 cyc/条 —— 平坦, 且**独立重现了 E1 的 122**。
    #   ★ 为什么不是 E1 旧值 1376: 1376 是 E1 在"128 条 div0"配置下的**基线拍**,
    #     它含该配置特有的拍内工作; 用作线性模型的截距会带来 **−185 cyc** 的系统偏差
    #     （实测 |偏差| = 47 个标准误 —— 统计上极显著, 但那是**常数选错**, 不是模型错）。
    #     ⇒ 教训: **"截距"必须与"斜率"在同一组配置下标定**, 不能跨配置借用。
    ap.add_argument("--c0", type=float, default=1200.0,
                    help="拍内固定开销 CPU cyc（div1 标定, n=16/128 两点法定标）")
    ap.add_argument("--c-route", type=float, default=121.2, dest="c_route",
                    help="每条路由的平均增量 CPU cyc（div1 标定, 三段斜率 120.6~122.3）")
    ap.add_argument("--cpu-hz", type=float, default=400e6, dest="cpu_hz")
    ap.add_argument("--tb-hz", type=float, default=200e6, dest="tb_hz",
                    help="时基频率（TIM5=200 MHz）；`di` 的单位是它的 tick")
    ap.add_argument("--sigma", type=float, default=3.0,
                    help="均值判据的容差（标准误的倍数）")
    # ★★ 黑匣子对照（`0x39 CMD_PIN_PATTERN` op=4）: `di` 量的是**整段 ISR**, 除引擎扫描外
    #   还含 `bb_kick()`（每拍一次 AXI 写 + 每 1024 拍的重活）、`adc_poll_kick()`、
    #   `rtc_latch()`、`i2c_sm_tick()`。要谈"周期的精确计算", 必须先把**引擎扫描**从
    #   这些拍内工作里分出来 —— 否则测到的是"ISR 有多长", 不是"程序要跑多少周期"。
    #   关掉黑匣子是固件**自带**的对照开关, 代价 0, 直接可用。
    # ★★ 默认从 `keep` 改成 `on`: `g_bb_off` 住在 **RAM**, 一个 `--bb off` 的窗口结束后
    #   **它会一直留着**, 于是"下一次运行"会在**没人知道**的配置下测 —— 正是本项目
    #   "同一个语义两处存放 ⇒ 静默失效"的同族。现在**每次运行都显式置一次**,
    #   板子被留在**确定**状态, 且把该状态打进日志（P0c）。
    ap.add_argument("--bb", choices=("on", "off"), default="on",
                    help="黑匣子 bb_kick 开关（**每次运行都显式置位**, 因它住在 RAM）")
    a = ap.parse_args()
    unit = a.cpu_hz / a.tb_hz      # TB tick → CPU cycle

    dcl = Dcl(a.port)
    print("端口 = %s" % dcl.port); time.sleep(1.0)
    res = []

    # ★★★ 2026-09-18: **自证探针 —— 已撤, 保留其结论**。
    #   它当年放在"任何其它动作之前", 靠 `SHM_SEEN = 0x20004E80` 硬编码地址
    #   （理由: 取 shm 的唯一办法是读 0x38, 所以"不发 0x38"就必须硬编码）。
    #   ★ 结论（仍然有效, 已写进 `_wait_healthy` 与文档）: **未发任何帧**时
    #     `tick` 已经是 78 ⇒ 板子在打开端口前/那一刻就在复位—恢复态里,
    #     与本工具的收发顺序无关。
    #   ★ 撤掉的理由: 加 E-D 的 SCAN 域后 `g_shm` 从 `0x20004E80` 移到 `0x20004EA0`,
    #     硬编码**静默失效** —— 板上一切正常（samples 在涨、run=1）, 而工具报"不健康"。
    #     这是 §5.42「工具的隐含前提会随固件改变而静默失效」的第二次实证。
    #   ⇒ 现在**先问 0x38 拿地址, 再做健康门**（见下）。

    # ★★ 顺序修正（2026-09-18, 第二次）: **先问 0x38 拿 SHM 地址, 再做健康门**。
    #   原版把"自证探针"放在 0x38 之前, 靠 `SHM_SEEN = 0x20004E80` 硬编码。
    #   加 E-D 的 SCAN 域之后 `g_shm` 移到 `0x20004EA0` ⇒ 硬编码**静默失效**,
    #   症状是"板子不健康 ⇒ 判无效", 而板上 `samples` 在涨、`run=1`, **完全正常**。
    #   ⇒ 教训: **同一块板换个构建, 硬编码地址就会静默失效** —— 与 §5.42
    #     "工具的隐含前提会随固件改变而静默失效"是同一族, 这次是它第二次实证。
    #   ⇒ 地址一律问 0x38（那正是它的用途）。探针的历史使命已完成（见 `_wait_healthy`）。
    #   ★ 结论（2026-09-18）: 那条"0x38 之前的探针"一次就把元凶抓到了 —— **未发任何帧**时
    #     `tick` 已经是 78 ⇒ 板子在打开端口前/那一刻就在复位—恢复态里。
    #     ⇒ 补上 `_wait_healthy()`（见其 docstring: 判据、为什么能失败、怎么强制复位）。
    #     ★ 探针本身**已撤**（它靠硬编码地址, 会随构建静默失效）—— 见下面的顺序修正。

    def _peek(tag, shm_now):
        v = _tick_of(dcl, shm_now)
        print("    [自证] %-20s 环写=%-8s tick=%s"
              % (tag, v[0] if v else "?", v[1] if v else "?"))
        return v

    try:
        sts, p = dcl.send(cmd_status, expect_len=51)
        if sts != "ACK" or len(p) < 51:
            print("!! 0x38 失败"); return 2
        shm = struct.unpack("<I", p[23:27])[0]
        print("SHM = 0x%08X" % shm)
        if not _wait_healthy(dcl, shm):
            dcl.close()
            print("\n[exit] 板子不健康 ⇒ 判无效（这不是本工具的失败, 是**不测**）")
            return 2
        _peek("② 0x38 之后", shm)

        def probe(tag):
            """★ 给工具自己装探针: 在**它自己的序列**里分点看四个计数器。
            第一版没有这个, 于是"窗口只有 78 拍"只能靠猜; 探针（别的序列）全都正常
            ⇒ 必须让工具自证在哪一步被清零。"""
            h = rd(dcl, shm + OFF_EXEC_RING_HDR, 2)
            sts2, s2 = dcl.send(cmd_status, expect_len=51)
            smp = struct.unpack("<I", s2[:4])[0] if sts2 == "ACK" else -1
            nr = struct.unpack("<H", s2[20:22])[0] if sts2 == "ACK" else -1
            print("    [探针] %-22s 环写=%-8s tick=%-10s samples=%-8s n_routes=%s"
                  % (tag, h[0] if h else "?", h[1] if h else "?", smp, nr))
            return h

        # ★★★ 2026-09-18: **回到"已被证明能工作的最小序列"**。
        #   六个假设全部排除后（§A3.8），症状被定为"板子被某个动作带进复位—恢复态"。
        #   而 `probe_ring5` 阶段 3 已证明下面这条**最小序列**是健康的:
        #       STOP → START → deploy → STOP → START → 静默 1.5 s → 读
        #       ⇒ 环写 = 15037（正是预期的 ~15000）
        #   ⇒ 先**绕开**（本工具不再在序列中间做任何额外的 0x38/探针），
        #     把"哪个动作让主循环停了一次"降级成一个**独立的、不阻塞 E4 的**问题。
        #   ★ 保留一条健康判据: `tick`（自由计数, **不随 stats_reset 归零**）
        #     必须单调递增到百万级; 掉回两位数 ⇒ 板子在复位循环里 ⇒ **判无效**。
        op, div, n = (int(x, 0) for x in a.prog.split(","))
        sts, pp = dcl.send(cmd_deploy, mk(op, div, n), expect_len=None)
        if sts != "ACK":
            print("!! deploy 被拒: %s" % (pp.decode('utf-8', 'replace') if sts == 'NAK' else sts))
            return 2
        print("已部署 op=%d div=%d n=%d ⇒ ACK budget=%d"
              % (op, div, n, struct.unpack("<HI", pp[:6])[1]))

        # STOP/START ⇒ 清统计 + 清环, 保留程序（**这是最小序列里唯一的 STOP/START 对**）
        _peek("③ deploy 之后", shm)
        # ★ 黑匣子对照: 必须在 STOP/START **之前**设, 且在 deploy 之后（不影响部署本身）。
        #   判读: `--bb off` 与 `--bb on` 两次的均值之差 = 黑匣子每拍的净成本。
        #   ★ 每次都显式置位（见 argparse 处的说明）⇒ P0c 记录**本次窗口**的配置,
        #     它不是"猜的", 是"刚设的"。
        off = 1 if a.bb == "off" else 0
        sts3, _ = dcl.send(0x39, bytes([4, off]), expect_len=None)
        res.append(("P0c 黑匣子已显式置为 %s（0x39 op=4 ACK）" % a.bb, sts3 == "ACK"))
        print("黑匣子: 置为 **%s** ⇒ 0x39 op=4 [on=%d] → %s" % (a.bb, off, sts3))
        dcl.send(cmd_stop); time.sleep(0.15)
        dcl.send(cmd_start); time.sleep(0.15)
        _peek("④ START 之后 0.15s", shm)
        time.sleep(a.settle)
        _peek("⑤ 静默 %.2fs 后" % a.settle, shm)

        # ★★ 顺序修正（第一版踩过）: **先读头、再读环、再读头**。
        #   第一版是"先读环再读头", 结果两次都读到 `写计数=57 / tick=69` —— 而探针
        #   （读 2 个字, 不做大读）显示环以 1:1 于 RUN 拍推进、10.3 kHz。
        #   ⇒ 问题在**读数时序**: 大 `0x22` 读(256 字 = 2 块 ≈140 ms)会阻塞主循环,
        #     让头里读到的是"读之后"的状态。现在读头两次, 不一致就**判无效**而不是硬算。
        #   ★ 同时读 `0x38.samples`, 用它**独立核对窗口大小**（不许只信一个计数器）。
        def hdr():
            """读环头 4 个字（写计数 / 最后一拍的 tick / 槽数 / 保留）+ `0x38.samples`。

            ★★ 2026-09-18 记录一段**走偏三版**的排查, 因为它很典型:
              现象是"同一个地址 `0x20008700`、同一条命令 `0x22`, 2 字读对、4 字读给出另一个
              看着也像计数器的值"。前三版一直在**解释数字**（先后怀疑地址算错、板块状态、
              字长路径), 直到打出**线上原始字节**才看清: 4 字读**也是对的**, 只是它和
              2 字读**在时间上相差约 30 µs**, 而这两个计数器每 100 µs 就 +1
              ⇒ 差值 99/100 是**正常的采样间隔**, 不是数据错。
            ⇒ 教训:**"两次读数不一致"必须先问"它们是不是同时刻的"**。
              用"不一致就报警"去抓时序差, 会一直报警, 而且会把人引向不存在的路径。
              （本项目既有纪律"同一个语义两处存放 ⇒ 静默失效"的**反面**: 这里是
                两个**必然不同**的采样被当成"同一时刻的两个副本"去比对。）
            ⇒ 所以本函数只读一次, 不再做自比对。
            """
            raw = rd(dcl, shm + OFF_EXEC_RING_HDR, 4)
            h = struct.unpack("<4I", raw) if raw and len(raw) >= 16 else None
            sts2, s2 = dcl.send(cmd_status, expect_len=51)
            smp = struct.unpack("<I", s2[:4])[0] if sts2 == "ACK" else -1
            return (h, smp)

        h1, smp1 = hdr()
        ring = rd(dcl, shm + OFF_EXEC_RING, RING_SLOTS)
        h2, smp2 = hdr()
        if ring is None or h1 is None or h2 is None:
            print("!! 读失败 ⇒ 判无效"); return 2
        w, last_tick, slots = h2[0], h2[1], h2[2]
        print("环头: 写计数=%d tick=%d（读环前写计数=%d）" % (w, last_tick, h1[0]))
        # ★ 健康判据: `tick` 是**自由计数**, 不随 stats_reset 归零 ⇒
        #   健康的板子上它应该在百万级; 掉回两位数说明板子在复位循环里。
        res.append(("P0a 板子健康（tick 在百万级，非复位循环）", last_tick > 100000))
        if w == 0:
            print("!! 写计数为 0 ⇒ 环没在动 ⇒ 判无效"); return 2
        res.append(("P0 环缓冲在动（写计数 > 0）", True))
        # ★ 窗口自检: 环写计数必须与 RUN 拍数**同量级**（1:1 附近）
        #   —— 第一版就是缺这一条, 才会拿一个 68 拍的窗口去算频次。
        if smp2 > 0 and w > 0:
            ratio = w / float(smp2) if smp2 else 0
            print("   环写/RUN拍 = %.3f（应 ≈1.000；实测探针 0.999~1.004）" % ratio)
            res.append(("P0b 环写计数 ≈ RUN 拍数（0.9~1.1）", 0.9 <= ratio <= 1.1))

        # 取出最近 min(w, slots) 条, 并回推每条的 tick
        cnt = min(w, RING_SLOTS)
        vals, ticks = [], []
        for k in range(cnt):
            idx = (w - cnt + k) & (RING_SLOTS - 1)
            vals.append(struct.unpack("<I", ring[idx * 4:idx * 4 + 4])[0])
            ticks.append(last_tick - (cnt - 1 - k))
        hist = Counter(vals)
        print("\n逐拍 di 的取值分布（共 %d 拍，跨度 %d..%d）:" % (cnt, ticks[0], ticks[-1]))
        for v, c in sorted(hist.items()):
            print("    di=%-7d 出现 %4d 次  (%.1f%%)" % (v, c, 100.0 * c / cnt))

        # ★★ 窗口下限闸门（第一版缺这一条, 于是拿 **3 拍**的窗口跑出了 P1/P2/P3 三个
        #   "PASS" —— 因为预测频次也一起缩到 3, 于是"看起来对上了"。**假绿**。
        #   本项目纪律: 覆盖不到判 **SKIP, 不许读成 PASS**。窗口至少要盖住 2 个调度周期
        #   （div2 的周期是 100 拍 ⇒ 取 200 拍）。
        MIN_WIN = 200
        if cnt < MIN_WIN:
            print("\n  [SKIP] 窗口只有 %d 拍 < %d ⇒ **判无效, 不评 P1/P2/P3**" % (cnt, MIN_WIN))
            print("     为什么: 频次判据在小子样上会被「等比例缩小」骗过去 —— 那是假绿。")
            print("     这条闸门正是被第一版的一次 3 拍窗口换来的。")
            for k, v in res:
                print("  [%s] %s" % ("PASS" if v else "FAIL", k))
            print("  [SKIP] P1/P2/P3 —— 窗口不足, **不是 PASS**")
            return 2

        # ── 结构预测: nrun(t) = n_div0 + cnt1[t%10] + cnt2[t%100] ─────────
        bk = rd(dcl, shm + OFF_ROUTE_BUCKETS, 110)
        rt = rd(dcl, shm + OFF_ROUTE_TABLE, 128 * 4)
        if bk is None or rt is None:
            print("!! 读桶表/路由表失败 ⇒ 判无效"); return 2
        u = struct.unpack("<220H", bk)
        off1, cnt1 = list(u[0:10]), list(u[10:20])
        off2, cnt2 = list(u[20:120]), list(u[120:220])
        nr = off1[0] + sum(cnt1) + sum(cnt2)
        n0 = off1[0]
        pred_nrun = Counter(n0 + cnt1[t % B1P] + cnt2[t % B2P] for t in range(100))
        print("\n结构预测（一个 100 拍周期内）: n_routes=%d  div0=%d" % (nr, n0))
        for k, c in sorted(pred_nrun.items()):
            print("    本拍跑 %-4d 条 ⇒ 占 %3d/100 拍" % (k, c))

        # ★★★ 2026-09-18 判据改写：**原来的 P1/P2/P3 被数据推翻了**。
        #
        #   原判据（"实测取值个数与频次 == 结构预测的 nrun 取值个数与频次"）隐含一个
        #   **从未声明过的假设**: `di` 是 `nrun` 的**确定性函数**（每个 nrun 恰好一个 di）。
        #   实测（本工具第一次拿到健康窗口, 256 拍, div1×128 规则）:
        #       结构预测: 2 个取值 —— 跑 12 条占 20/100 拍、跑 13 条占 80/100 拍
        #       实测    : **13 个**取值, 主峰 1380(145 次) 与 1318(36 次) 之外
        #                 还有 1352/1400/1412/1482 … 一串
        #   ⇒ 判据 FAIL 是**正确的**: 它推翻的是一个真假设。`di` = **整段 ISR** 的时长,
        #     除了引擎扫描, 还含黑匣子快照（每 1024 拍一次重活）、ADC 启动、RTC 锁存、
        #     I2C 状态机、以及逐拍的数据相关分支 ⇒ **拍内其它工作 + 分支差异** 都是噪声源。
        #   ⇒ 这不是"测量失败", 而是**测量第一次好到能证伪**。
        #
        #   ⇒ 换成**模型真正主张的那一条**（线性成本模型直接给出的可失败预言）:
        #         E[di] ≈ c₀ + c_route × E[nrun]
        #     `c₀`（拍内固定开销）与 `c_route`（每条路由的平均增量）由**独立实验**
        #     标定（E1 两点法: c_route = 122 cyc/条, 基线 ISR c₀ ≈ 1376 cyc），
        #     **不是**从本窗口拟合出来的 ⇒ 这是**预测**而不是自证。
        #   ★ 为什么用"窗口均值"而不是"逐值配对"（后者更严但做不了）:
        #     逐值配对要求把每个 di 归到具体的 nrun, 而 nrun 只在**知道该拍的相位**时
        #     才成立; 环里只有 di, 没有相位。相位可以从 tick 推（t%10 / t%100）,
        #     但那要求"读数期间拍号不丢" —— 大读会阻塞主循环约 60 ms（≈600 拍）,
        #     丢拍是**已知**的（这正是 P0b 比值 0.988 而不是 1.000 的原因）。
        #   ★ 均值判据为什么仍然**能失败**: 它把"哪些拍贵"这个问题放过, 只问
        #     "贵的平均起来对不对"。若 c_route 或桶调度任一处错了, 均值必然偏 ——
        #     而分母（拍数）是独立数出来的**实测**值, 不与模型共享。
        obs = sorted(hist.items())
        pn = sorted(pred_nrun.items())
        mean_nrun = sum(k * v for k, v in pred_nrun.items()) / 100.0
        mu = sum(vals) / float(cnt)
        var = sum((x - mu) ** 2 for x in vals) / float(cnt - 1) if cnt > 1 else 0.0
        sd = var ** 0.5
        sem = sd / (cnt ** 0.5) if cnt else 0.0
        pred_mean = (a.c0 + a.c_route * mean_nrun) / unit      # cyc → TB tick
        print("\n模型预言（常数由 E1 两点法独立标定, **非本窗口拟合**）:")
        print("    单位换算: %g cyc/TB tick（CPU %g Hz ÷ TB %g Hz）" % (unit, a.cpu_hz, a.tb_hz))
        print("    结构预测: nrun ∈ %s  ⇒ 平均每拍跑 %.2f 条"
              % ([k for k, _ in pn], mean_nrun))
        print("    预言 E[di] = (c0 + c_route×E[nrun]) / unit = (%.0f + %.1f×%.2f)/%g = **%.0f TB tick**"
              % (a.c0, a.c_route, mean_nrun, unit, pred_mean))
        print("    实测 di  : 均值 %.1f TB tick  标准差 %.1f  标准误 %.1f  (n=%d)"
              % (mu, sd, sem, cnt))
        print("    换成 CPU cyc: 实测 %.0f  预言 %.0f  ⇒ 偏差 %+.0f cyc"
              % (mu * unit, pred_mean * unit, (mu - pred_mean) * unit))
        tol = a.sigma * sem
        dev = mu - pred_mean
        print("    偏差 = %+.1f cyc = %+.2f 标准误（判据: |偏差| ≤ %.2f 标准误）"
              % (dev, dev / sem if sem else 0.0, a.sigma))
        # ★ 取值个数只作**描述**打印, 不再当判据 —— 它测的是"噪声有多少", 不是"模型对不对"。
        print("    参考: 实测 %d 个不同取值（模型只预言 %d 个档位）—— 差额即拍内其它工作"
              % (len(obs), len(pn)))

        res.append(("P1 实测窗口均值落在模型预言 ±%.1f 标准误内" % a.sigma, abs(dev) <= tol))
        if a.json:
            import json
            os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
            json.dump(dict(prog=a.prog, cnt=cnt, hist=dict(hist),
                           pred_nrun=dict(pred_nrun), nr=nr, n0=n0,
                           first_tick=ticks[0], last_tick=ticks[-1]),
                      open(a.json, "w"), indent=1)
            print("\n原始数据: %s" % a.json)
    finally:
        dcl.send(cmd_stop); time.sleep(0.2); dcl.send(cmd_start); time.sleep(0.3)
        dcl.close()

    print("\n=== 断言 ===")
    for k, v in res:
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    bad = [k for k, v in res if not v]
    print("\n%d 项, %d FAIL" % (len(res), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
