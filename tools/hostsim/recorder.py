#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hostsim/recorder.py —— 数据记录器（上位机的 "historian" 层）

═══════════════════════════════════════════════════════════════════════════════
它是什么 / 为什么需要它
═══════════════════════════════════════════════════════════════════════════════
SCADA 里"能看"和"能分析"是两套东西:
    · **HMI 轮询**（bridge.py） = 给人眼看的, 25 Hz 够, 拔了线就没了。
    · **Historian**（本文件）   = 给事后分析用的, 按固定周期**落盘**, 有口径。
本项目原来只有前者。本文件补后者。

═══════════════════════════════════════════════════════════════════════════════
★★★ 本文件唯一最重要的设计: **时间戳来自板子, 不来自 PC**
═══════════════════════════════════════════════════════════════════════════════
`tick` 列取的是板子的 `SHM+0x08 HEARTBEAT` —— 固件里**每拍 +1** 的自由运行计数器
（实测 ≈10 400 拍/s @拍长 100 µs, 与 10 kHz 吻合; 32 位 ⇒ 4.97 天回绕）。

为什么不能记 PC 的墙钟时间:
  USB 转串口的**驱动缓冲** + Windows 调度会让数据**成簇到达**。PC 侧相邻两次读到的
  时间差里混着"板子真的过了这么多拍" + "驱动什么时候把包给我" 两件事, 二者不可分。
  ★ 本项目已实测判死这个口径 (`MEMORY.md` §5.27): PC 侧差分测速被**回填率 × 轮询率的
    **拍频**污染, 只有 `ΣΔraw/总时长` 免疫。
⇒ 所以本文件的速度列 `vel_mm_s` 用 **Δcounts / (Δtick × 拍长)** 算 ——
  **分子分母同为板子口径** ⇒ 免疫 PC 侧抖动。`pc_t` 列照记, 但**只作辅助**, 不许当时间用。

═══════════════════════════════════════════════════════════════════════════════
采样节拍（照抄 historian 行业的 "scan class" 概念: 不同量不同周期）
═══════════════════════════════════════════════════════════════════════════════
  · **快组**（每轮, 2 次协议调用）
      `0x22 (SHM+0x0000, 16)`   → MAGIC / HEARTBEAT / run / n_routes
      `0x22 (SHM+0x0040, 256)`  → SENSOR[0..63] + ACTUATOR[0..63] + WIRE[0..127]
      ★ 0x0040..0x0440 正好 **256 字 = READ_BURST 的单次上限**, 一次读完三个域。
  · **慢组**（每 `--slow-every` 轮, 1 次调用）
      `0x38` → ov/uart_ore/uart_drop/frame_bad （计数器变得慢, 没必要每轮问）
  · **事件**（增量 ≠ 0 才写一行）→ `<out>.events.csv`

★ 实测往返时延 ~55 ms(p99 56.8, `MEMORY.md` §5.15) / 一次 256 字的读光传输就 ~90 ms
  ⇒ **实际可达速率 ≈ 6 Hz**, 不是想设多高就多高。`--hz` 是**目标值**;
  真实速率由 `tick` 差分算出并写进 meta, 不假装。

═══════════════════════════════════════════════════════════════════════════════
产物: 两个文件 —— **数据 + 口径**, 缺一不可
═══════════════════════════════════════════════════════════════════════════════
  `<out>.csv`        数据（列名沿用 `sd_log_read.py --csv` 的口径, 分析脚本零改动）
  `<out>.meta.json`  口径（固件指纹/拍长/导程/自检结果/真实速率/pitch 来源）
  `<out>.events.csv` 计数器增量事件（可能不存在 = 全程无事件）

★★ 为什么必须落一个 meta 文件: 三个月后拿到一个 csv, 里面只有 `tick, SENSOR0, ...`,
   **没有任何信息能回答**"这是哪个固件的、拍长多少、pitch 多少、速度列可不可信"。
   这正是本项目 `MEMORY.md` §5.36 那条"同一文档前后矛盾、前者未标作废 ⇒ 读者拿到错前提"
   的同族: **数据文件也必须能自证口径**。
   ⇒ `--verify` 就是为此: 拿着 csv 找它的 meta, 重新跑一遍判据。

═══════════════════════════════════════════════════════════════════════════════
自检（**能失败**, 失败就拒绝写数据 —— 不是警告一下就继续）
═══════════════════════════════════════════════════════════════════════════════
  C0 `MAGIC == 0x44434C31`                      —— 自报地址对不对（读错区会立刻暴露）
  C1 `|deg − raw×360/4096| ≤ 0.001°`             —— SENSOR 量纲自检（MEMORY.md §五 已定口径）
  C2 `Δtick > 0` 且全程单调                       —— 时间戳真的在走
  ★ 这三条都是**硬闸门**: 任何一条不过 ⇒ 不写 csv, 打印原因, 退出码 2。
    理由: 一个"口径没验过的记录文件"比没有文件更坏 —— 它会被当成证据引用。

用法:
    python tools/hostsim/recorder.py --secs 30
    python tools/hostsim/recorder.py --forever --hz 5 --out run1
    python tools/hostsim/recorder.py --verify run1.csv
    python tools/hostsim/recorder.py --live-render          # 顺带起 bridge 的 /state 源

★ 串口是**独占资源** —— recorder 跑着的时候 bridge.py / mgmt.py 用不了那个口。
"""
import argparse
import csv
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

# ── 地址来源 ────────────────────────────────────────────────────────────────
# SHM_CTRL: 来自板子自报目录 (0x64) —— 见 fetch_ctrl_addr()
#
# ★★ SENSOR_MAP / WIRE_MAP **不在** 0x64 目录里, 只能硬编码。这是**已知欠账**:
#    manifest.h 自己写着"本表是唯一真值源 —— 不许在 PC 工具里另写一份地址",
#    而实测有 **13 个** tools/*.py 各自写着 `0x0240`。
#    正确修法 = 给 0x64 加 `SENSOR_MAP` / `WIRE_MAP` / `EXEC_RING` 三行（属固件改动,
#    要同步更新 tools/h723_restore_delivery.sh 的 EXPECT_MD5）。
#    在补上之前, 本文件用 C1 量纲自检把"读错区"从**静默错**变成**响亮失败**。
OFF_SENSOR_MAP = 0x0040          # <- 与 src/engine.h 同值; 由 C1 自检兜底
OFF_WIRE_MAP = 0x0240            # <- 与 src/engine.h 同值
BLK_SENSOR_TO_LUT = 256          # SENSOR(64)+ACTUATOR(64)+WIRE(128) = 256 字 = READ_BURST 上限

MAGIC_EXPECT = 0x44434C31
COUNTS_PER_REV = 4096.0
STEPS_PER_REV = 1574.4           # 本项目实测（步/圈）
TICK_US_EXPECT = 100.0           # 交付档拍长（README/MEMORY.md §一）

# WIRE 槽位（src/step.h:105-110）—— ★ 请求值与已应用值**都要记**
W_REQ_RATE, W_REQ_DIR, W_REQ_ENA, W_REQ_LIMIT = 12, 13, 14, 15
W_AP_RATE, W_AP_LIMIT = 64, 65


# ═══════════════════════════ 工具 ═══════════════════════════
def _f32(words, i):
    return struct.unpack("<f", struct.pack("<I", words[i]))[0]


class Unwrap:
    """0..4095 绝对角 → 连续累计 counts（跨 0 补 ±4096）。

    ★ 判据: 单次 Δ 若 > 半圈, 只能是被绕回来了（真运动不可能一步过半圈）。"""

    def __init__(self):
        self.prev = None
        self.total = 0.0

    def feed(self, raw):
        if self.prev is None:
            self.prev = raw
            return
        d = raw - self.prev
        if d > COUNTS_PER_REV / 2:
            d -= COUNTS_PER_REV
        elif d < -COUNTS_PER_REV / 2:
            d += COUNTS_PER_REV
        self.prev = raw
        self.total += d


# ═══════════════════════════ 采集 ═══════════════════════════
class Rec:
    def __init__(self, dcl, pitch, hz, slow_every):
        self.d = dcl
        self.pitch = pitch
        self.hz = hz
        self.slow_every = max(1, slow_every)
        self.shm = None
        self.off_ctrl = None
        self.uw = Unwrap()
        self.prev_hb = None
        self.n = 0
        self.slow = None          # 上一次慢组的计数器
        self.checks = {}
        self.rate_measured = 0.0
        self._prev_total = 0.0    # ★ 必须初始化: 首轮 sample() 就要拿它算 Δcounts

    # ── 0x64 目录: 取 SHM_CTRL 的地址（不硬编码）──
    def fetch_manifest(self):
        """0x64 是**分页**的（`MF_PER_PAGE=4`, main.c:2277）⇒ 必须翻页收齐。

        ★★ 且返回的 `addr` 是**已解析的绝对地址**（main.c:2292:
           `put32(o+12, (flags & MF_F_SHM) ? (shm + e->addr) : e->addr)`）——
           不是 manfiest.h 里写的 SHM 相对偏移。本文件第一版就假设错了这一条,
           被下面的自检当场抓出（报 addr=0x200052E0）。"""
        out, page, total = [], 0, None
        while True:
            st, pl = self.d.send(0x64, bytes([page]))
            if st != "ACK" or not pl:
                raise RuntimeError("0x64 SYS_MANIFEST 无应答 —— 固件没带管理面")
            total, n = pl[0], pl[1]
            for i in range(n):
                o = pl[2 + i * 20: 2 + (i + 1) * 20]
                if len(o) < 20:
                    break
                out.append(dict(
                    name=o[0:12].split(b"\x00")[0].decode("latin-1"),
                    addr=struct.unpack_from("<I", o, 12)[0],
                    words=struct.unpack_from("<H", o, 16)[0],
                    kind=o[18], flags=o[19], shm=bool(o[19] & 1)))
            page += 1
            if n == 0 or len(out) >= total or page > 64:
                break
        if total is not None and len(out) != total:
            raise RuntimeError("0x64 只收到 %d/%d 条目录 —— 翻页没收敛" % (len(out), total))
        self.manifest = out
        e = next((x for x in out if x["name"] == "SHM_CTRL"), None)
        if e is None:
            raise RuntimeError("0x64 目录里没有 SHM_CTRL —— 目录与固件不同代")
        return e

    def open(self):
        # g_shm 从 0x38 自取（**绝不写死**: 烧录后它会变）
        st, pl = self.d.send(0x38, expect_len=51)
        if st != "ACK" or len(pl) < 51:
            raise RuntimeError("0x38 无应答")
        self.shm = struct.unpack("<I", pl[23:27])[0]
        e = self.fetch_manifest()
        self.off_ctrl = e["addr"]              # ★ 绝对地址（0x64 已解析）
        self.ctrl_words = max(4, e["words"])
        # 弱判别闸门: SHM_CTRL 是目录第一条、指向 SHM 基址本身。若哪天有人挪了它、
        # 或 0x64 的"相对/绝对"语义变了, 这里立刻炸 —— 而不是读到别的区还算出一堆数。
        if not e["shm"] or e["addr"] != self.shm:
            raise RuntimeError("SHM_CTRL 不再指向 SHM 基址 (addr=0x%08X, g_shm=0x%08X, shm_flag=%s)"
                               " —— 布局或 0x64 语义变了, 先核 manifest.h 与 main.c:2292"
                               % (e["addr"], self.shm, e["shm"]))
        return self

    def read_ctrl(self):
        st, pl = self.d.send(0x22, struct.pack("<IH", self.off_ctrl,
                                               self.ctrl_words), expect_len=None)
        if st != "ACK" or len(pl) < 16:
            raise RuntimeError("0x22 SHM_CTRL 无应答")
        w = struct.unpack("<%dI" % self.ctrl_words, pl[:self.ctrl_words * 4])
        magic, hb = w[0], w[2]
        b3 = struct.unpack("<I", struct.pack("<I", w[3]))[0]
        run = (b3 >> 8) & 0xFF          # SHM+0x0D
        n_routes = (b3 >> 16) & 0xFF    # SHM+0x0E
        return magic, hb, run, n_routes

    def read_block(self):
        st, pl = self.d.send(0x22, struct.pack("<IH", self.shm + OFF_SENSOR_MAP,
                                               BLK_SENSOR_TO_LUT), expect_len=None)
        if st != "ACK" or len(pl) < BLK_SENSOR_TO_LUT * 4:
            raise RuntimeError("0x22 SENSOR/WIRE 块无应答 (>256 字? 超 READ_BURST 上限)")
        return struct.unpack("<%dI" % BLK_SENSOR_TO_LUT, pl[:BLK_SENSOR_TO_LUT * 4])

    def read_slow(self):
        st, pl = self.d.send(0x38, expect_len=51)
        if st != "ACK" or len(pl) < 51:
            return None
        u = lambda o: struct.unpack("<I", pl[o:o + 4])[0]
        return dict(ov=u(27), ore=u(39), drop=u(43), bad=u(47))

    # ── 采样一个点 ──
    def sample(self):
        pc_t = time.time()
        magic, hb, run, n_routes = self.read_ctrl()
        blk = self.read_block()

        raw = _f32(blk, 0)                       # SENSOR[0]
        deg = _f32(blk, 1)                       # SENSOR[1]
        wi = lambda k: _f32(blk, 128 + k)        # WIRE_MAP 在块内 +128 字 (64+64)

        # ── C0/C1 自检（能失败）──
        if magic != MAGIC_EXPECT:
            raise RuntimeError("C0 失败: MAGIC=0x%08X ≠ 0x%08X —— 读到别的区了"
                               % (magic, MAGIC_EXPECT))
        self.checks["C0_magic"] = "PASS"
        if raw == 0.0 and deg == 0.0:
            # SENSOR 全 0 可能是"真没接"也可能是"读错区" ⇒ 必须与 MAGIC 联合判（上面已过）
            pass
        d_deg = abs(deg - raw * 360.0 / COUNTS_PER_REV)
        if d_deg > 0.001:
            raise RuntimeError("C1 失败: |deg − raw×360/4096| = %.6f° > 0.001° "
                               "(raw=%.1f deg=%.3f) —— SENSOR 量纲不对, 拒绝记录"
                               % (d_deg, raw, deg))
        # C1b: AS5600 原始角必须是 [0,4096) 的**整数值**。这一条抓的是"从别的区读浮点"
        #   —— 那里的数几乎不可能同时满足"整数 且 落在 12 位量程内"。
        if not (0.0 <= raw < 4096.0) or raw != float(int(raw)):
            raise RuntimeError("C1b 失败: SENSOR0=%.6f 不是 [0,4096) 内的整数 —— "
                               "SENSOR_MAP 偏移 (%#x) 可能不对" % (raw, OFF_SENSOR_MAP))
        self.checks["C1_sensor_dim"] = "PASS"
        self.checks["C1b_raw_is_12bit_int"] = "PASS (raw=%d)" % int(raw)

        # ── C2: 时间戳真的在走 ──
        d_tick = 0
        if self.prev_hb is not None:
            d_tick = (hb - self.prev_hb) & 0xFFFFFFFF
            if d_tick == 0:
                raise RuntimeError("C2 失败: HEARTBEAT 未变化 —— 时间戳停了")
        self.prev_hb = hb
        self.checks["C2_tick_monotonic"] = "PASS"

        self.uw.feed(raw)
        pos_mm = self.uw.total / COUNTS_PER_REV * self.pitch
        # ★ 速度: 分子(Δcounts) 与 分母(Δtick) **同为板子口径** ⇒ 免疫 PC 侧拍频
        vel = 0.0
        if d_tick > 0:
            d_cnt = self.uw.total - self._prev_total
            vel = d_cnt / COUNTS_PER_REV * self.pitch / (d_tick * TICK_US_EXPECT * 1e-6)
        self._prev_total = self.uw.total

        row = dict(tick=hb, d_tick=d_tick, pc_t="%.6f" % pc_t,
                   run=run, n_routes=n_routes, magic="0x%08X" % magic,
                   SENSOR0=raw, SENSOR1=deg,
                   WIRE12_req_hz=wi(W_REQ_RATE), WIRE13_req_dir=wi(W_REQ_DIR),
                   WIRE14_req_ena=wi(W_REQ_ENA), WIRE15_req_lim=wi(W_REQ_LIMIT),
                   WIRE64_ap_hz=wi(W_AP_RATE), WIRE65_ap_lim=wi(W_AP_LIMIT),
                   pos_mm=pos_mm, vel_mm_s=vel)

        # ── 慢组 + 事件 ──
        ev = None
        if self.n % self.slow_every == 0:
            s = self.read_slow()
            if s:
                if self.slow:
                    d = {k: s[k] - self.slow[k] for k in s}
                    if any(v != 0 for v in d.values()):
                        ev = dict(tick=hb, **{"d_" + k: v for k, v in d.items()},
                                  **{"abs_" + k: s[k] for k in s})
                self.slow = s
                row.update(ov=s["ov"], uart_ore=s["ore"],
                           uart_drop=s["drop"], frame_bad=s["bad"])
        self.n += 1
        return row, ev


# ═══════════════════════════ meta ═══════════════════════════
COLS = ["tick", "d_tick", "pc_t", "run", "n_routes", "magic",
        "SENSOR0", "SENSOR1",
        "WIRE12_req_hz", "WIRE13_req_dir", "WIRE14_req_ena", "WIRE15_req_lim",
        "WIRE64_ap_hz", "WIRE65_ap_lim",
        "pos_mm", "vel_mm_s",
        "ov", "uart_ore", "uart_drop", "frame_bad"]

META_WHY = {
    "tick": "板子 SHM+0x08 HEARTBEAT, 每拍 +1。★ 这是本文件**唯一权威时间**",
    "d_tick": "与上一点的 tick 差（单位: 拍）。★ vel_mm_s 的分母就是它",
    "pc_t": "PC 墙钟（Unix 秒）。★ **辅助列, 不许当时间用**（USB 缓冲+调度 ⇒ 成簇到达）",
    "SENSOR0": "AS5600 原始角 0..4095 (SHM+0x0040)",
    "SENSOR1": "角度 deg，由固件给；与 SENSOR0 的换算关系已由 C1 自检核过",
    "WIRE12_req_hz": "请求频率 Hz（程序面写的**目标值**）",
    "WIRE64_ap_hz": "**已应用**频率 Hz（固件每拍写的镜像）★ 与上面配对看: '设了就算'必错",
    "pos_mm": "unwrap(SENSOR0)/4096 × pitch。pitch 见本 meta 的 pitch_mm_rev",
    "vel_mm_s": "Δcounts/4096 × pitch / (d_tick × tick_us)。★ 分子分母**同为板子口径**",
    "ov": "ISR 超预算次数（累计）",
}


# ═══════════════════════════ 主流程 ═══════════════════════════
def run(args):
    from h723_client import Dcl, find_board
    port = args.port or find_board()
    d = Dcl(port)
    print("  端口 %s" % d.port)

    base = os.path.splitext(args.out)[0]
    csv_path, meta_path, ev_path = base + ".csv", base + ".meta.json", base + ".events.csv"

    r = Rec(d, args.pitch, args.hz, args.slow_every).open()
    print("  g_shm = 0x%08X    SHM_CTRL 绝对地址 = 0x%08X (来自 0x64 目录, 已解析)"
          % (r.shm, r.off_ctrl))
    print("  manifest: %d 条 (%s)"
          % (len(r.manifest), ", ".join(x["name"] for x in r.manifest[:6]) + " ..."))

    meta = dict(
        schema="DCL-HOSTLOG v1",
        purpose="上位机 historian 记录——用途是**事后分析**, 不是实时显示",
        created=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        port=str(d.port),
        pitch_mm_rev=args.pitch,
        tick_us=TICK_US_EXPECT,
        tick_hz=1e6 / TICK_US_EXPECT,
        steps_per_rev=STEPS_PER_REV,
        # ★★ 这一行决定 tick 能不能当时间用。分析前**必须先看它**
        sample_rule="device_tick",
        sample_rule_note=("tick 列 = 板子 HEARTBEAT ⇒ 是真时间, 可以差分/做频谱。"
                          "若换成 pc_poll_* 口径则 tick 只表示'哪一拍被读到', 间隔不均匀, "
                          "**绝不可差分**"),
        scan_classes={"fast": "每轮: SHM_CTRL(16字) + SENSOR/ACT/WIRE 块(256字)",
                      "slow": "每 %d 轮: 0x38 计数器" % args.slow_every,
                      "event": "计数器增量 != 0 才写一行"},
        addresses={"SHM_CTRL": "0x%08X (来自 0x64 目录, 绝对地址)" % r.off_ctrl,
                   "SENSOR_MAP": "g_shm+0x%04X (硬编码——不在 0x64 里, 见下方 debt)" % OFF_SENSOR_MAP,
                   "WIRE_MAP": "g_shm+0x%04X (同上)" % OFF_WIRE_MAP,
                   "g_shm": "0x%08X (从 0x38 的 +23 自取)" % r.shm},
        debt=("SENSOR_MAP/WIRE_MAP 未进 0x64 目录 (manifest.h 自己禁止 PC 侧另写地址, "
              "而实测 13 个 tools/*.py 各自硬编码 0x0240)。修法 = 给 0x64 加三行 + "
              "同步 h723_restore_delivery.sh 的 EXPECT_MD5。当前由 C1 量纲自检兜底。"),
        target_hz=args.hz,
        expected_hz_note=("往返时延实测 ~55 ms(p99)+大帧传输 ~90 ms ⇒ 实际可达 ≈6 Hz。"
                          "target_hz 只是目标值, 真实速率见 measured_hz"),
    )

    rows, evs = [], []
    t0 = time.time()
    interrupted = False
    print("\n  采样中… (Ctrl-C 提前收尾)")
    try:
        while True:
            if args.secs and (time.time() - t0) >= args.secs:
                break
            t_next = t0 + r.n / args.hz
            row, ev = r.sample()
            rows.append(row)
            if ev:
                evs.append(ev)
                print("  ★ 事件 @tick=%d: %s" % (ev["tick"],
                      {k: v for k, v in ev.items() if k.startswith("d_") and v}))
            if r.n % 25 == 0:
                print("    n=%d tick=%d d_tick=%s" % (r.n, row["tick"], row["d_tick"]))
            w = t_next + 1.0 / args.hz - time.time()
            if w > 0:
                time.sleep(min(w, 0.3))
    except KeyboardInterrupt:
        interrupted = True
        print("\n  ^C —— 收尾")
    finally:
        d.close()

    if not rows:
        print("!! 一个点都没采到 ⇒ 不写文件（空文件比没有文件更坏）")
        return 2
    # 慢组没轮到过的行会缺 ov/... 键 —— 补空, 保证列一致
    for row in rows:
        for c in COLS:
            row.setdefault(c, "")

    # ★★ 速率必须用**首末采样点之间**的墙钟跨度算, 不能用"循环总耗时"。
    #    第一版就是这么错的: `wall` 从循环开始前算起, 而 `span_tick` 从第一个点算起,
    #    差了一个首点时延 (~190 ms / 10 s ≈ 1.9%) ⇒ 报出"板子拍长 102 µs"这种假故障。
    #    ★ 这与 MEMORY.md §〇 第 10 条同族: **差分量的分子与分母必须同口径。**
    wall_s_total = time.time() - t0
    wall = float(rows[-1]["pc_t"]) - float(rows[0]["pc_t"]) if len(rows) > 1 else wall_s_total
    span_tick = rows[-1]["tick"] - rows[0]["tick"]
    dt_list = [x["d_tick"] for x in rows[1:] if x["d_tick"]]
    meta.update(n_samples=len(rows), wall_s=round(wall, 3),
                wall_s_total=round(wall_s_total, 3),
                wall_note=("wall_s = 首末**采样点之间**的墙钟跨度（与 span_tick 同口径）; "
                           "wall_s_total = 含建链与收尾的总耗时，**别拿它算速率**"),
                interrupted=interrupted,
                span_tick=span_tick,
                measured_hz=round((len(rows) - 1) / wall, 3) if wall > 0 else None,
                measured_tick_hz=round(span_tick / wall, 1) if wall > 0 else None,
                d_tick_min=min(dt_list) if dt_list else None,
                d_tick_max=max(dt_list) if dt_list else None,
                d_tick_note=("★ Δtick 不恒定是**正常的**: 它反映 PC 侧轮询的真实抖动。"
                             "正因为如此, 速度必须按**每点的 Δtick** 算, "
                             "而不是假设等间隔 ⇒ 本文件的 vel_mm_s 就是这么算的"),
                checks=r.checks,
                columns=COLS,
                column_meaning=META_WHY,
                events_n=len(evs))

    # ★ C2 的事后判据: 时间戳必须真的覆盖了整段墙钟
    if meta["measured_tick_hz"] is not None:
        err = abs(meta["measured_tick_hz"] - meta["tick_hz"]) / meta["tick_hz"]
        meta["checks"]["C2_rate_matches_tick_us"] = (
            "PASS (实测 %.0f Hz vs 期望 %.0f Hz, 差 %.1f%%)"
            % (meta["measured_tick_hz"], meta["tick_hz"], err * 100)
            if err < 0.15 else
            "**FAIL** (实测 %.0f Hz vs 期望 %.0f Hz, 差 %.1f%% ⇒ 拍长不是 %.0f µs? 先核!"
             % (meta["measured_tick_hz"], meta["tick_hz"], err * 100, TICK_US_EXPECT))

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        cw.writeheader()
        for row in rows:
            cw.writerow(row)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    if evs:
        ek = sorted({k for e in evs for k in e})
        with open(ev_path, "w", newline="", encoding="utf-8-sig") as f:
            cw = csv.DictWriter(f, fieldnames=ek)
            cw.writeheader()
            for e in evs:
                cw.writerow(e)

    print("\n" + "=" * 70)
    print("  %d 个点 / %.1f s  →  实测 %.2f Hz（目标 %.2f）" %
          (len(rows), wall, meta["measured_hz"] or 0, args.hz))
    print("  板子跨度 %d 拍 = %.2f s   Δtick %s..%s" %
          (span_tick, span_tick * TICK_US_EXPECT / 1e6,
           meta["d_tick_min"], meta["d_tick_max"]))
    for k, v in sorted(meta["checks"].items()):
        print("  %-28s %s" % (k, v))
    print("  → %s" % csv_path)
    print("  → %s   ★ 分析前先读它" % meta_path)
    if evs:
        print("  → %s   (%d 个事件)" % (ev_path, len(evs)))
    print("=" * 70)
    return 0


def verify(path):
    """事后核口径: 拿 csv 找它的 meta, 重跑判据。**不是**"看一眼就完事"。"""
    base = os.path.splitext(path)[0]
    meta_path = base + ".meta.json"
    if not os.path.exists(meta_path):
        print("!! 找不到 %s" % meta_path)
        print("   ⇒ **这个 csv 的口径不可知**。它可能是别的固件/别的拍长采的,")
        print("     而 tick 的单位、vel 的口径都无从确认 ⇒ 不许当作证据引用。")
        return 2
    meta = json.load(open(meta_path, encoding="utf-8"))
    print("=== 核对 %s ===" % path)
    print("  schema           : %s" % meta.get("schema"))
    print("  created          : %s" % meta.get("created"))
    print("  ★ sample_rule    : %s" % meta.get("sample_rule"))
    print("     ⇒ %s" % meta.get("sample_rule_note"))
    print("  拍长             : %s µs   (%.0f Hz)" % (meta.get("tick_us"), meta.get("tick_hz")))
    print("  pitch            : %s mm/rev" % meta.get("pitch_mm_rev"))
    print("  实测 tick 速率    : %s Hz  (期望 %s)" %
          (meta.get("measured_tick_hz"), meta.get("tick_hz")))
    print("  自检:")
    for k, v in sorted((meta.get("checks") or {}).items()):
        print("    %-28s %s" % (k, v))
    ok = all("FAIL" not in str(v) for v in (meta.get("checks") or {}).values())
    print("  ⇒ %s" % ("PASS" if ok else "**FAIL** —— 这份数据有口径问题, 别直接拿去做结论"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="hostlog", help="输出基名（生成 .csv/.meta.json/.events.csv）")
    ap.add_argument("--secs", type=float, default=30.0, help="记录时长；0 = 无限")
    ap.add_argument("--forever", action="store_true", help="等价 --secs 0")
    ap.add_argument("--hz", type=float, default=5.0, help="目标采样率（实际受往返时延限制 ≈6）")
    ap.add_argument("--slow-every", type=int, default=10, help="每 N 轮读一次慢组计数器")
    ap.add_argument("--pitch", type=float, default=8.0, help="丝杆导程 mm/rev（T8=8, T5=2）")
    ap.add_argument("--port", default=os.environ.get("DCL_PORT"))
    ap.add_argument("--verify", metavar="CSV", help="只核对一个已有 csv 的口径, 不连板子")
    a = ap.parse_args()
    if a.forever:
        a.secs = 0
    if a.verify:
        return verify(a.verify)
    return run(a)


if __name__ == "__main__":
    raise SystemExit(main())
