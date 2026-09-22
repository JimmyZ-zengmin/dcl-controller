#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hostsim/bridge.py —— 运动可视化上位机的"桥接层"

把**板子的真实运动**变成浏览器能画的数据。

## 为什么不用现成的运动控制上位机
查过：现有的（Sophon / SmartMotion / 各种 Qt·WinForm 步进上位机）全是
**"发指令 + 画曲线"**型，**没有一个做机构可视化**；而且协议是 Modbus-RTU 之类，
**接不到本项目的 DCL 协议上** ⇒ 桥接层无论如何都得自己写。

## 架构（零第三方依赖）
    [板子 COMxx]  --pyserial-->  bridge.py（内置 http.server）
                                      ├── GET /        → index.html
                                      └── GET /state   → JSON 状态
    [浏览器]  fetch('/state') 每 40 ms → three.js 更新 电机 / 丝杆 / 滑台

## 两种数据源
    --live COM21        实时读板子（`0x22` 读 SENSOR[0]/[1] + WIRE[12]）
    --csv  tri.csv      回放 SD 日志（吃 `tools/sd_log_read.py --csv` 的产物）

## 解算（这就是"电机 → 丝杆 → 滑台"的物理绑定）
    raw = SENSOR[0]            AS5600 12 位绝对角，0..4095
    Δ   解绕（跨 0 边界补 ±4096）→ 累计 counts
    revs = counts / 4096        每圈 4096 counts
    pos  = revs × pitch         pitch = 丝杆导程 mm/rev（默认 8.0，T8 常见）

## ★★★ 速度的两个来源（2026-09-22 定案）
    vel_mm_s = Δcounts / (Δtick × 拍长) × pitch    ← **设备口径**（免疫 PC 侧拍频）
                 Δtick = SHM+0x08 HEARTBEAT 的差（每拍 +1，实测 10 001 Hz vs 期望 10 000）
                 ★ 改前用的是 PC 墙钟 `dt` —— 那是噪声，显示的"速度抖动"大半是采集口径
    ap_mm_s  = WIRE[64] / 1574.4 × pitch          ← **板子每拍算的"已应用频率"**，最权威
                 ★ 它与 WIRE[12]（请求值）**配对看**："设了就算"必错（MEMORY.md §〇 第 6 条）
    ⇒ 画"实际速度"用 `ap_mm_s`；`vel_mm_s` 留作交叉验证。

★ 串口是**独占资源** —— bridge 跑着的时候别的工具用不了那个口。

用法：
    python tools/hostsim/bridge.py --live COM21 --pitch 8
    python tools/hostsim/bridge.py --csv tri.csv --pitch 8
    # 浏览器开 http://127.0.0.1:8765
"""
import argparse
import csv
import json
import math
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))

COUNTS_PER_REV = 4096.0      # AS5600 12 位
STEPS_PER_REV = 1574.4       # 本项目实测（步/圈）
OFF_SENSOR, OFF_WIRE = 0x0040, 0x0240
OFF_CTRL = 0x0000            # SHM_CTRL: +0x08 = HEARTBEAT(每拍+1) ← ★ 唯一权威时间源
TICK_US = 100.0              # 交付档拍长（改拍长要同步这里; meta 里有自检会炸）
# ★★ 这三个偏移是**硬编码**的 —— 它们**不在 0x64 自报目录里**（已知欠账, 见 MEMORY.md §26.6）。
#   正确修法 = 给 0x64 加 SENSOR_MAP / WIRE_MAP 三行（固件改动, 要同步改 EXPECT_MD5）。
#   在补上之前靠 `sanity()` 兜底: 一旦读错区立刻报错, 而不是把别的数当位置画出来。
W_REQ_RATE = 12          # WIRE[12] = ③层程序写的**请求**频率 Hz（仅"程序面"模式下有意义）

# ══════════ ★★★ 增量流（DELTA_RING）—— 2026-09-22 起的主数据通路 ══════════
SUB_DELTA_READ = 26      # 0x39 op=19 sub=26, arg = from_seq
DELTA_HDR_OFF = 0x7FC0   # OFF_DELTA_HDR（SHM 内）
DELTA_HDR_N = 5          # [0]w [1]tick [2]drop [3]掩码低 [4]掩码高

# ★ 通道映射表（与 `src/blackbox.c` 的 `s_bb_map_def` 逐项对应），每项 = (seg, idx)。
#  ★★ 为什么敢在 PC 侧再写一份：因为**它自带对账** —— 固件用 FNV-1a 把表算成校验和
#     放在 `BB_DIAG[37]`（`bb_map_sum()`），下面 `bb_map_fnv()` 用**同一算法**算本地表，
#     不一致就**拒绝运行**（不是警告）。这正是设计者的意图（见 `blackbox.c` 的注释：
#     "固件与 PC 端同一算法，用来判'表头和固件是不是同一份映射'"）。
#  ⇒ 所以这是"**有校验的副本**"，不是"两处各写一遍"。
BB_SEG_SENSOR, BB_SEG_WIRE, BB_SEG_ACT = 0, 1, 2
BB_SEG_TIME, BB_SEG_COMM, BB_SEG_DO, BB_SEG_FORCE, BB_SEG_FAULT = 4, 5, 6, 7, 8
BB_MAP = ([(BB_SEG_SENSOR, i) for i in range(14)] + [(BB_SEG_FAULT, 0), (BB_SEG_FAULT, 1)]
          + [(BB_SEG_WIRE, i) for i in range(16)] + [(BB_SEG_ACT, i) for i in range(16)]
          + [(BB_SEG_TIME, 0), (BB_SEG_TIME, 1)] + [(BB_SEG_COMM, i) for i in range(5)]
          + [(BB_SEG_DO, 0)] + [(BB_SEG_FORCE, i) for i in range(4)])
assert len(BB_MAP) == 60, len(BB_MAP)
# ★ 运动面用到的槽号（都是"映射槽号"，不是 SHM 偏移）
SLOT_RAW, SLOT_DEG, SLOT_WIRE0 = 0, 1, 16

# ══════════ ★★★ HIL 反向通道（PC → 板子）══════════
# 用户的构想: **"把虚拟环境当作位置传感器返回给板子 ⇒ 就能做位置环"**。
#   ★ 它绕过的正是本项目最硬的边界: **AS5600 是单圈绝对编码器** ⇒
#     ③ 层拿不到多圈位置（见 `examples/h723_step_screw_recip.dcl` 的说明）。
#     PC 侧已经在累计绝对位置（`WAVEBUF`）⇒ 写回板子 ⇒ ③ 层就"看得见"全行程了。
#   ★ 通道选 `wire[50]`（不是 `SENSOR[]`）: `SENSOR[0..10]` **全都有写者**
#     （AS5600/HIL/DI/AI）⇒ PC 再写同一个槽 = 两个写者（违反单写者纪律）。
#     `wire[0..15]` 是 HMI 约定、`wire[64..]` 是固件保留 ⇒ 取 16..63 里的 50。
#   ★★ **代价必须说清**: 更新率 = 本循环（~10 Hz）⇒ 位置环带宽上限 **~5 Hz**,
#      而且 100 ms 的开环推进容易过冲 ⇒ 它验证的是"位置域算法与总线闭环的逻辑",
#      **不是"实时位置环的性能"**（那需要板内多圈绝对位置）。
HIL_VIRT_POS_SLOT = 50          # 写 wire[50] = 虚拟绝对位置 (mm)
HIL_VIRT_POS_ON = os.environ.get("HIL_VIRT_POS", "0") == "1"

# ══════════════════ ★★★ 管理面（"下位机仪表盘"）══════════════════
# 需求（用户 2026-09-22）：**"把曾经藏在代码里、需要 pyocd 看的任务，
#   搬到上位机可以直接看到的地方。"**
#
# ★ 板子**本来就有**这个能力 —— `src/manifest.h` 是"板内诊断资源目录"，它的注释
#   把动机写得很清楚（原文）:
#     · "**消灭『PC 端硬编码地址』这一整类缺陷**"（今天两次踩到：硬编码 SHM 基址
#       读出台账 magic=0 的假故障；手抄各诊断区偏移）
#     · "**回答『哪里出问题读什么』** —— 诊断知识的落点从**人的记忆**搬到**代码里**"
#     · "**它是看门狗的前置**"（复位归因要的 BOOT_AXI 在 AXI，靠只读窗 + 本目录
#       即可走常规通信读到，**不需要调试器**）
#   协议口 = `0x64`（**分页**发目录）+ `0x22`（按名字读值），客户端 = `tools/mgmt.py`。
# ★ 缺的只是**它没接进网页**（只能命令行跑 `python tools/mgmt.py --health`）。
# ⇒ 这里把 mgmt 的**判读能力直接复用**（`verdict()` / `layout_check()` 都是纯函数，
#   且 `mgmt.py` 有 `__main__` 保护 ⇒ import 安全），而 **IO 用 bridge 自己的 `Dcl`**：
#   串口是独占资源，另开一个句柄 = 本项目已踩过的坑。
# ★★ 代价必须说清：管理面读是**多条往返**（health 清单 8 条 ⇒ ~9 次 × ~10 ms ≈ 90 ms）
#   ⇒ 所以**低频轮询**（默认 5 s ⇒ 平均占用串口 ~1.8%）。
#   ★ 它不会停引擎：只读 + 定长 + 不写 SD + 不等外设（`mgmt.py` 的三条硬规矩之一）。
MGMT_AUTO_S = float(os.environ.get("MGMT_AUTO_S", "5.0"))   # 0 = 关闭自动轮询
# ★ 自动轮询的**精简集** = `mgmt.py --health` 选的那 8 条（"最先该看的"）。
#   `SHM_CTRL` 单列（要读两次判活，见下）。
MGMT_NAMES = ("FAULTLOG", "WDT_STAT", "MB_DIAG", "MB_CTRL",
              "BB_DIAG", "RTC_DIAG", "BOOT_AXI")
# 按需刷新请求（用户点"刷新"）—— 由 live 线程执行（串口独占）
MGMTQ = []

# ══════════ ★★★ 通道直方图（2026-09-22 长稳分析的直接产出）══════════
# 为什么要有它：长稳实测"运动时产出 419 条/s，而消费上限只有 ~300~330 ⇒ 必丢 20%"。
#   而 `engine.h` 里有一条实测：**加大单次批长并不提高消费**
#   （n=16/24/32 = 281/297/283 条/s，非单调；n=64 时单次往返 198.7 ms ⇒ 只有 322 条/s）
#   ⇒ **"提高消费"这条路已经走到头**（瓶颈是"主循环发大块"的固有代价，不是批长）。
# ⇒ 于是唯一剩下的杠杆是**降低产出**，而前提是**先知道 419 条里谁占大头**。
#   ★ 这与"AI 三路(槽 8/9/10)+WIRE[9] 各 620 Hz 是 ADC 噪声不是状态"是同一类发现 ——
#     上次靠人翻代码找到，这次**让数据自己说**。
# ★ 产出 = 各通道变化率之和 ⇒ 直方图就是"产出分解表"。


def bb_slot_name(ch):
    """槽号 → 可读名（用 BB_MAP 的 seg/index 反推，避免另写一份映射）。"""
    try:
        seg, idx = BB_MAP[ch]
    except Exception:
        return "ch%d" % ch
    if seg == BB_SEG_SENSOR:
        return "SENSOR[%d]" % idx
    if seg == BB_SEG_FAULT:
        return "FAULT[%d]" % idx
    if seg == BB_SEG_TIME:
        return "TIME[%d]" % idx
    if seg == BB_SEG_COMM:
        return "COMM[%d]" % idx
    if seg == BB_SEG_DO:
        return "DO[%d]" % idx
    if seg == BB_SEG_FORCE:
        return "FORCE[%d]" % idx
    return "seg%d[%d]" % (seg, idx)

# 判读逻辑复用（拿了就能把原始数变成结论；拿不到就退化成"只给值"）
try:
    if os.path.join(ROOT, "tools") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
    import mgmt as _mgmt            # noqa: E402
except Exception:                   # ★ 绝不让它把 bridge 拖死（mgmt.py 顶层 import serial，
    _mgmt = None                    #   失败时会 print + sys.exit(2)）


def bb_map_fnv():
    """FNV-1a 32bit —— 与固件 `bb_map_sum()` 逐位相同（用于和 BB_DIAG[37] 对账）。"""
    h = 2166136261
    for seg, idx in BB_MAP:
        h ^= (((seg << 16) | idx) & 0xFFFFFFFF)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


# ★★ 度数值**从 raw 推**（不依赖通道 1 被上传）。
#   理由见 `engine.h` 的 `DELTA_MASK_DEF_LO`：槽 1(deg) 与槽 0(raw) 一一对应，
#   固件已把它关掉省带宽（`deg = raw*360/4096`，与 `as_write_shm` 同一算法）。
#   ★ 用「从 raw 推」而不是「读镜像里的旧 deg」，是因为后者**只在打底时新鲜**。
def _deg_from_raw(raw):
    return raw * 360.0 / COUNTS_PER_REV

# ★★ 不要用 `WIRE[64]`（STEP_MOT_SLOT_RATE_AP）当"已应用频率"读：
#   它由 `step_service_motion()` 写，而那函数在**脚手架模式下第一行就 return**（step.c:684）
#   ⇒ 那个槽在此模式下**从不刷新**，读到的是残留值（实测在 251~3762 之间乱跳，
#     看着像"频率失控"，其实是读了个没人维护的槽）。
#   权威来源 = `0x39 op=19 sub=14` 的 +16（同一个 `g_step_rate_hz`，且与运动源无关）。

STATE = {
    "ok": False, "src": "-", "msg": "未启动",
    "t": 0.0, "raw": 0.0, "deg": 0.0, "revs": 0.0, "pos_mm": 0.0,
    "vel_mm_s": 0.0, "cmd_hz": 0.0, "cmd_mm_s": 0.0,
    "cmd_eff_hz": 0.0, "cmd_eff_mm_s": 0.0,   # ★ 按运动源自适应的"命令"（曲线用这个）
    "ap_hz": 0.0, "ap_mm_s": 0.0,
    "mosrc": "-", "cmd_n": 0, "applied_n": 0, "rej_n": 0, "lim_rem": 0,
    "req_hz": 0.0, "src_mode": "-", "lim_ms": 0, "mismatch": False,
    "tick": 0, "d_tick": 0, "dt_s": 0.0, "rate": 0.0,
    "run": 0, "ov": 0, "pitch": 8.0, "n": 0, "src_rule": "-",
    # ══════════ ★★★ 层级对账（2026-09-22）══════════
    # 用户的指正：「**你这里层级关系没处理好，上位机与板子之间做的不是很系统**」。
    # 具体缺陷（实测抓到）: `/state` 里 `src_mode=scaffold` 而 `mosrc=program`
    #   —— 前者是**上位机自己记的意图**（"我发过什么"），后者是**板子实读的**
    #   （`sub=14 +0`）。**两者矛盾而界面只显示前者** ⇒ 人看到的是错的。
    # ⇒ 正解 = 把两个来源**并列给出**，并给出**一致性判据**。
    #   ★ 这就是本项目"设了就算必错"（§〇 第 6 条）在上位机侧的实例。
    "src_intent": "-",        # 上位机**意图**打的源（/motion?src=）
    "src_dev": "-",           # 板子**实读**的源（sub=14 的 +0）
    "src_match": True,        # 两者是否一致
    # 程序面的三个可读环节 —— "停了又跑"能一眼定位到哪一环没关：
    "wire_ena": None,         # ★ wire[10] **实读**（程序使能；③层程序读它）
    "wire_req": None,         # ★ wire[12] **实读**（③层程序写的请求频率）
    "scaf_hz": None,          # ★ wire[12] 同槽在脚手架模式下无人维护（仅程序面有意义）
    # ══════════ ★ 停机验证（点停之后自动回答"到底停住没有"）══════════
    # ★ 为什么必须自动验: 用户实测"点停后短暂停了又跑" ⇒ **"发了停机命令" ≠ "停下了"**
    #   （同族：§〇 第 6 条）。上位机有责任在 N 秒后**读回**并给出结论。
    "stop_t": 0.0, "stop_ok": None, "stop_msg": "—",
}
LOCK = threading.Lock()
CMDQ = []            # 浏览器下发的控制命令，由 live 线程（持有串口的那个）排空
# ★ 为什么走队列：串口是独占资源，控制命令**必须由同一个句柄**发出 ——
#   另开一个进程去开同一个口 = 本项目已踩过的"外层没关就去开第二个句柄"。

# ══════════ ★★★ HIL 坐标原点（2026-09-22）══════════
# 用户构想：**"虚拟环境负责绝对位置坐标系，电机相当于半个在世界"**。
#   ★ 但"坐标系"必须有一个**双方同意的原点**，否则就是两个坐标系：
#     `Unwrap.total` 的起点 = **bridge 进程启动时刻**（从 0 起累加），
#     而③层程序的目标（0/60/120 mm）是**它自己的坐标系**。
#   ★★ 实测踩到（2026-09-22）：bridge 重启后 PC 位置已累计到 **1921 mm**，
#     而起跑时程序目标仍是 0 ⇒ 误差 −1800 mm ⇒ `LIMIT MX=3000` 钳满 ⇒
#     **电机满速往回跑 95 秒**（`ap_hz=3003`）。这不是"控制坏了"，是**坐标系没对齐**。
#   ⇒ 所以 `/zero` 是 HIL 的**契约组成部分**（不是 UI 便利功能）：
#     把 PC 当前绝对位置**定义**为 0，并**立刻**把新的 `wire[50]` 写回板子
#     （不等下一轮 —— 否则中间那拍板子仍拿到旧值）。
#   ★ 前端原来的"行程归零"**只改本地显示偏移**（`zeroOffset`），**根本不碰板子**
#     ⇒ 在 HIL 下它是**空操作**。现已改为打 `/zero`。
ZEROQ = []           # 模块级请求队列：live 线程执行（它持有串口与 Unwrap）

# ══════════ ★★★ 全分辨率位置序列（用户的判据：「演示可以低分辨率，计算必须全量」）══════════
# ★ 这是与"状态镜像"并列的**第二个消费者**，两个都来自同一份事件流：
#     · mirror[60]  → 当前状态，给**渲染**（60 fps 取最近值即可，本来就不需要插值）
#     · WAVEBUF[]   → 每一个 SENSOR[0] 变化点 (tick, pos_mm)，给**计算**
#                     （速度/加速度/波形都从这里算 —— 实测 100 Hz，而不是轮询抽样的 12.5 Hz）
# ★ 上限 900 点 ≈ 9 s @100 Hz。取 9 s 是因为前端波形窗口是 12 s，
#   宁可让前端"要更长的历史"时去读 CSV，也不要在这里无限增长。
WAVEBUF = []         # [(tick, pos_mm)]
WAVE_MAX = 900


class Unwrap:
    """把 0..4095 的绝对角解开成连续累计 counts（跨 0 补 ±4096）。

    ★ 判据：单次 Δ 若 > 半圈，只能是被绕回来了（真运动不可能一步过半圈）。"""

    def __init__(self):
        self.prev = None
        self.total = 0.0

    def feed(self, raw):
        if self.prev is None:
            self.prev = raw
            return 0.0
        d = raw - self.prev
        if d > COUNTS_PER_REV / 2:
            d -= COUNTS_PER_REV
        elif d < -COUNTS_PER_REV / 2:
            d += COUNTS_PER_REV
        self.prev = raw
        self.total += d
        return d

    def reset(self):
        self.prev = None
        self.total = 0.0


# ══════════ ★★★ 管理面读原语（0x64 目录 / 0x22 按名字读）══════════
def mgmt_manifest(d):
    """读板子**自报**的诊断目录（`0x64`）。

    ★ 分页格式（`src/main.c` 的 `h_manifest`，`MF_PER_PAGE=4`）:
      请求 = `[page u8]`；应答 = `[总条目数 u8][本页条数 u8]` + 每页 n×20 B，
      每条 20 B = `name[12]`(定长 ASCII, 补 0) + `addr:u32` + `words:u16` + `kind:u8` + `flags:u8`。
      ★ `addr` 已被**固件解析成真实地址**（SHM 相对项已加 `g_shm`）⇒ PC 端**不做任何地址算术**
        —— 这正是这张表存在的理由（"布局怎么挪都不会读错"）。
    ★ 与 `mgmt.Board.manifest()` 是同一份逻辑，但走 bridge 的 `Dcl`（串口独占）。
    """
    out, page, total = [], 0, None
    while True:
        st, pl = d.send(0x64, bytes([page]), expect_len=None)
        if st != "ACK" or not pl or len(pl) < 2:
            raise RuntimeError("0x64 无应答 —— 固件没带管理面（或链路不通）")
        total = pl[0]
        cnt = pl[1]
        for i in range(cnt):
            o = pl[2 + i * 20:2 + (i + 1) * 20]
            if len(o) < 20:
                break
            out.append({
                "name": o[0:12].split(b"\x00")[0].decode("latin-1"),
                "addr": struct.unpack_from("<I", o, 12)[0],
                "words": struct.unpack_from("<H", o, 16)[0],
                "kind": o[18], "flags": o[19],
            })
        page += 1
        if len(out) >= total or cnt == 0 or page > 64:
            break
    return out


def mgmt_read(d, e):
    """按目录条目读值（`0x22` READ_BURST [addr:u32][count:u16]）。"""
    st, pl = d.send(0x22, struct.pack("<IH", e["addr"], e["words"]), expect_len=None)
    if st != "ACK" or not pl or len(pl) < e["words"] * 4:
        raise RuntimeError("读 %s (0x%08X, %d 字) 被拒/无应答" % (e["name"], e["addr"], e["words"]))
    return [struct.unpack_from("<I", pl, i * 4)[0] for i in range(e["words"])]


def mgmt_poll(d, cache):
    """跑一轮"健检"，结果写进 `cache`（dict）。

    ★ 顺序照 `mgmt.py --health`：**先判活**（HEARTBEAT 两次读数必须不同），
      再逐条判读。判活放第一是因为它决定"后面那些读到的数还算不算数"
      —— 引擎没跑时所有计数器都是冻的，读它们只会得出"一切正常"的假象。
    """
    if cache.get("manifest") is None:
        cache["manifest"] = mgmt_manifest(d)
    man = cache["manifest"]
    by = {x["name"].upper(): x for x in man}

    items = []
    # ① 存活：HEARTBEAT（SHM+0x08，每拍 +1）必须推进
    e = by.get("SHM_CTRL")
    if e is not None:
        try:
            w1 = mgmt_read(d, e)
            time.sleep(0.05)                 # ★ 短（原 mgmt.py 用 0.25 s；这里在实时循环里）
            w2 = mgmt_read(d, e)
            hb1, hb2 = w1[2], w2[2]
            alive = (hb2 != hb1)
            bad, lines = (not alive), [
                "HEARTBEAT %d → %d %s" % (hb1, hb2, "推进 ✓" if alive else "★不动 ⇒ ISR 没跑"),
                "MAGIC=0x%08X %s / VERSION=%d" % (w1[0], "OK" if w1[0] else "★空", w1[1]),
                "ENGINE_RUN 字节(SHM+0x0D)=0x%02X ⇒ 引擎 %s"
                % ((w2[3] >> 8) & 0xFF, "在跑" if ((w2[3] >> 8) & 1) else "已停"),
            ]
            items.append({"name": "SHM_CTRL", "kind": e["kind"], "bad": bool(bad),
                          "head": lines[0], "lines": lines})
        except Exception as ex:
            items.append({"name": "SHM_CTRL", "kind": e["kind"], "bad": True,
                          "head": "★读失败: %s" % ex, "lines": []})

    # ② 逐条判读（判读器来自 mgmt.py —— **不在上位机重写一份**，否则两份会漂）
    for nm in MGMT_NAMES:
        e = by.get(nm)
        if e is None:
            continue
        try:
            w = mgmt_read(d, e)
            if _mgmt is not None:
                # ★ 布局对账**在判读之前**（mgmt.py 的纪律）: 对不上就拒绝解析 ——
                #   否则给出的是"错位的数"，**比不解析更坏**（它会安静地给错结论）。
                lok, _ll = _mgmt.layout_check(nm, len(w))
                if not lok:
                    items.append({"name": nm, "kind": e["kind"], "bad": True,
                                  "head": "★布局对账失败 ⇒ 拒绝解析（见 mgmt.py --read %s）" % nm,
                                  "lines": []})
                    continue
                bad, lines = _mgmt.verdict(nm, e, w)
            else:
                bad, lines = False, ["(无判读器；原始值见 words) " +
                                     " ".join("%08X" % x for x in w[:6])]
            items.append({"name": nm, "kind": e["kind"], "bad": bool(bad),
                          "head": (lines[0] if lines else ""), "lines": lines})
        except Exception as ex:
            items.append({"name": nm, "kind": e.get("kind", 0), "bad": True,
                          "head": "★读失败: %s" % ex, "lines": []})

    cache["items"] = items
    cache["t"] = time.time()
    cache["err"] = None
    return cache


# ══════════════════ 实时源 ══════════════════
def run_live(port, pitch, hz):
    """实时源 —— ★ 2026-09-22 起主数据通路改成**增量事件流**（DELTA_RING）。

    ## 为什么改（这就是「全量数据做实时演示」的实现）
    旧做法：每轮 4 次**轮询抽样**（SENSOR[0..1] / WIRE[12] / sub=14 / 0x38）⇒ 12.5 Hz
            ⇒ 每 80 ms 只取 1 个位置，而编码器每 10 ms 变一次 ⇒ **丢 7/8**。
    新做法：每轮 `sub=26` 拉走**全部变化事件**（≤64 条），逐条应用到 60 槽**状态镜像**
            ⇒ 镜像 = 「板子那一拍的完整状态」，**无损**。

    ## ★★ 两个消费者（用户的判据：「演示可以低分辨率，计算必须全量」）
      · `mirror[60]` → 给**渲染**：60 fps 取最近值即可。★ **不需要插值** ——
        因为数据 100 Hz > 渲染 60 fps，每帧取「最近一次变化」本来就是精确的。
      · `WAVEBUF[]`  → 给**计算**：每个 `SENSOR[0]` 变化点存一条 (tick, pos_mm)，
        实测 **100 Hz** ⇒ 速度/波形都从这里算，而不是 12.5 Hz 抽样差分。

    ## 三条口径纪律（沿用）
    ① 时间戳来自板子（`SHM+0x08 HEARTBEAT` 每拍 +1）—— 权威时间。
    ② 「命令」按运动源自适应（程序面=WIRE[12] / 脚手架=本进程下发的 req_hz）。
    ③ 打底时做量纲自检（raw 是 [0,4096) 整数 + deg 与它自洽），不过就**响亮地停**。
    """
    from h723_client import Dcl
    d = Dcl(port)
    uw = Unwrap()
    with LOCK:
        STATE.update(src="live", msg="已连 %s" % d.port, pitch=pitch, ok=True)
        del WAVEBUF[:]

    shm = None
    mirror = [0] * 60          # ★ 60 槽状态镜像（u32 位型，与板子同表示）
    have_mirror = False
    from_seq = None
    prev_tick = None
    prev_wall = time.time()
    runv = ovv = 0
    tickv = 0
    n = 0
    err = 0
    evt_n = 0
    evt_rate = 0.0
    t_rate = time.time()
    drop_seen = 0
    # ★★ drop 的口径：`DELTA_HDR[2]` 是**累积**值（跨上电、跨所有消费者）⇒
    #   直接显示它会让人把「历史脏值」当成「本次丢了」。实测踩到：bridge 启动后
    #   显示 drop=3158 且恒定不涨，而那是我早先实验留下的累积值。
    #   ⇒ 记一个**启动基线**，对外只报增量。
    drop_base = None
    ap = 0.0
    mosrc = cmd_n = ap_n = rej_n = lim_rem = 0
    # ★ 管理面（下位机仪表盘）状态 —— 低频轮询，见 MGMT_AUTO_S 上方的长注释
    s_mgmt = {"manifest": None, "items": [], "t": 0.0, "err": "尚未轮询"}
    s_mgmt_t0 = 0.0
    # ★ 通道直方图（累积 1 s 后结算成"条/s"，再清零）—— 用于定位"谁把产出推高"
    s_ch_hist = {}
    s_ch_top = []
    while True:
        try:
            # ── 先排空浏览器下发的控制命令（必须在同一个串口句柄上发）──
            # ★★★ 2026-09-22 改造（用户指正"上位机与板子之间做的不是很系统"）：
            #   原来这里的两种写法**都会静默失败**，而静默失败在"停机"这种事上最危险：
            #     ① `elif kind == "wire" and shm:` —— **`shm` 未知时整条命令被丢掉**，
            #        连一句日志都没有。而 CMDQ 在**每轮开头**排空，第一轮 `shm` 必然是 None
            #        ⇒ 恰好在启动瞬间打的停机/写值会**人间蒸发**。
            #     ② `d.send(..., expect_len=None)` 的返回值**从不检查** ⇒
            #        固件 `sub=3` 的 **fail-closed NAK**（极性未声明 ⇒ 明确拒绝使能）
            #        到了上位机这边变成"发了但没反应" —— 而它的现象与"驱动器坏了"同形。
            #   ⇒ 现在就三条规矩：**不丢弃 · 看应答 · 被拒要能看见**。
            while CMDQ:
                kind, a1, a2 = CMDQ.pop(0)
                if kind == "pin":
                    sr, _sp = d.send(0x39, struct.pack("<BBI", 19, int(a1), int(a2)),
                                     expect_len=None)
                    if sr != "ACK":
                        with LOCK:
                            STATE.update(msg="★ 脚手架参数 sub=%d arg=%d 被拒: %s"
                                             % (int(a1), int(a2), sr))
                elif kind == "wire":
                    if shm is None:
                        # ★ 就地补读一次 SHM 基址（而不是丢弃）—— 0x38 应答 +23 是 shm。
                        s0, p0 = d.send(0x38, expect_len=51)
                        if s0 == "ACK" and len(p0) >= 51:
                            shm = struct.unpack("<I", p0[23:27])[0]
                    if shm is None:
                        with LOCK:
                            STATE.update(msg="★ wire[%d] 写入失败: 读不到 SHM 基址" % int(a1))
                    else:
                        bits = struct.unpack("<I", struct.pack("<f", float(a2)))[0]
                        sr, _sp = d.send(0x21, struct.pack("<II",
                                        shm + OFF_WIRE + int(a1) * 4, bits), expect_len=None)
                        if sr != "ACK":
                            with LOCK:
                                STATE.update(msg="★ wire[%d]=%g 写被拒: %s"
                                                 % (int(a1), float(a2), sr))

            # ── ★★★ HIL 坐标原点：把 PC 绝对位置归零，并**立刻**同步板子的 wire[50] ──
            #   ★ 为什么必须"立刻"写（而不是等下一轮的 HIL 反向通道）:
            #     `uw.reset()` 只改 PC 侧；板子的 `wire[50]` 仍是旧值 ⇒
            #     在下一轮 HIL 写到达前，③层程序会看到"归零前的位置" ⇒ 误差突变
            #     ⇒ 电机在那 1~2 拍里按错的误差推。（本项目的"设了就算"纪律：
            #     改了 PC 的量，就要把**它影响到的设备侧量**一起推过去。）
            #   ★ 顺序：先 reset uw → 再清 WAVEBUF → 再写板子。反了就有一拍用旧 pos。
            while ZEROQ:
                ZEROQ.pop(0)
                uw.reset()
                with LOCK:
                    del WAVEBUF[:]
                    STATE.update(zero_n=STATE.get("zero_n", 0) + 1,
                                 pos_mm=0.0, revs=0.0,
                                 msg="已归零：PC 绝对位置 → 0（板子 wire[50] 同步）")
                if shm:
                    d.send(0x21, struct.pack(
                        "<II", shm + OFF_WIRE + HIL_VIRT_POS_SLOT * 4,
                        struct.unpack("<I", struct.pack("<f", 0.0))[0]), expect_len=None)

            if shm is None or (n % 40 == 0):          # 0x38 慢组（run/ov 变得慢）
                s, p = d.send(0x38, expect_len=51)
                if s == "ACK" and len(p) >= 51:
                    shm = struct.unpack("<I", p[23:27])[0]
                    runv, ovv = p[22], struct.unpack("<I", p[27:31])[0]
                elif shm is None:
                    raise RuntimeError("0x38 no ACK")

            # ── ① 打底：只读运动面两段（各 16 字，一次性 ~30 ms）──
            #  ★ 为什么打底: 增量流只带「变过的通道」。若某运动通道上电后一直没变，
            #    镜像里它就是 0 ⇒ 前端会把「没变」误读成「值为 0」。
            if not have_mirror:
                for off, i0 in ((OFF_SENSOR, 0), (OFF_WIRE, 16)):
                    ss, pp = d.send(0x22, struct.pack("<IH", shm + off, 16), expect_len=None)
                    if ss != "ACK" or len(pp) < 64:
                        raise RuntimeError("打底读 0x%X 失败" % off)
                    w16 = struct.unpack("<16I", pp[:64])
                    for i in range(16):
                        mirror[i0 + i] = w16[i]
                r0 = _f32(mirror[SLOT_RAW])
                if not (0.0 <= r0 < 4096.0) or r0 != float(int(r0)):
                    raise RuntimeError("打底 SENSOR[0]=%.3f 不是 [0,4096) 整数 ⇒ 偏移不对" % r0)
                # ★★ 2026-09-22: **deg 的自洽检查去掉了** —— 因为固件侧已把映射槽 1（deg）
                #   从上传掩码里**关掉**（它与 raw 一一对应 `deg=raw*360/4096`，传两份是纯冗余；
                #   关掉让产出 315→215 条/s，才够协议侧的消费上限 ~297）。
                #   ⇒ `mirror[1]` 只在打底时是新鲜的，之后**必须从 raw 推**（见下面 DEG_FROM_RAW）。
                have_mirror = True

            # ── ② 首次从环底开始（一次拿满整个环 ⇒ 建立时间序列的起点）──
            if from_seq is None:
                sh, hp = d.send(0x22, struct.pack("<IH", shm + DELTA_HDR_OFF, DELTA_HDR_N),
                                expect_len=None)
                if sh != "ACK" or len(hp) < 20:
                    raise RuntimeError("读 DELTA_HDR 失败")
                hw, _htk, hdrop, mlo, mhi = struct.unpack("<5I", hp[:20])
                from_seq = (hw - 198) & 0xFFFFFFFF
                drop_base = hdrop          # ★ 启动基线（累积值）
                drop_seen = 0              # 对外报的是**增量**
                # ★ 掩码对账：本进程**依赖**运动通道不被滤掉。被滤了就直接停 ——
                #   否则现象是「3D 不动」，而人会去查接线/编码器，方向全错。
                for s_ in (SLOT_RAW, SLOT_DEG, SLOT_WIRE0):
                    on = ((mlo >> s_) & 1) if s_ < 32 else ((mhi >> (s_ - 32)) & 1)
                    if not on:
                        raise RuntimeError("掩码滤掉了运动通道 槽%d ⇒ 前端拿不到位置" % s_)

            # ── ③ 拉增量事件流（≤64 条），逐条应用到镜像 ──
            s5, p5 = d.send(0x39, struct.pack("<BBI", 19, SUB_DELTA_READ, from_seq), expect_len=None)
            if s5 != "ACK" or not p5 or len(p5) < 16:
                raise RuntimeError("sub=26 no ACK")
            cnt = p5[0]
            w_now, frm, drp = struct.unpack("<3I", p5[4:16])
            # ★★★ 2026-09-22 长稳分析的直接产出：本轮的"最后事件 tick"。
            #   为什么留它 —— 见下面 ④ 的注释：`0x22` 单读 tick 是**每轮 10 ms 的冗余往返**
            #   （`sub=26` 的每条事件**本身都带 tick**），而正是这 10 ms 让轮询率
            #   从 18 Hz 掉到 13 Hz ⇒ 单轮积压超过 `DELTA_READ_MAX(24)` ⇒ **环溢出丢条**。
            last_tk = None
            if drop_base is None:
                drop_base = drp
            # ★ 只报**自本次启动以来的增量**（见 drop_base 的注释）
            drop_seen = drp - drop_base
            for i in range(cnt):
                o = 16 + i * 16
                if len(p5) < o + 16:
                    break
                tk, _sq, ch, bits = struct.unpack("<4I", p5[o:o + 16])
                last_tk = tk                     # ★ 供 ④ 复用（省一次 0x22 往返）
                s_ch_hist[ch] = s_ch_hist.get(ch, 0) + 1   # ★ 产出分解（见 bb_slot_name）
                if ch >= 60:
                    continue
                mirror[ch] = bits
                # ★★ 每个 SENSOR[0]（编码器 raw）变化点 ⇒ 一条**全分辨率**位置采样。
                #    这就是「计算用全量数据」的落点：实测 100 Hz，不是 12.5 Hz。
                if ch == SLOT_RAW:
                    uw.feed(_f32(bits))
                    with LOCK:
                        WAVEBUF.append((tk, uw.total / COUNTS_PER_REV * pitch))
                        if len(WAVEBUF) > WAVE_MAX:
                            del WAVEBUF[:len(WAVEBUF) - WAVE_MAX]
            if cnt:
                from_seq = (frm + cnt) & 0xFFFFFFFF

            # ── ④ 板子的「现在」（权威时间）+ 事件率 ──
            # ★★★ 2026-09-22 优化（长稳分析的直接产出）——**有事件就不发这条命令**。
            #   实测依据：长稳 90 s 窗口内 轮询 13.1 Hz、产出 316 条/s、**丢 97 条/s**，
            #   而每轮只读走 16.7 条（上限 `DELTA_READ_MAX`=24）。
            #   ⇒ 丢的原因不是"读得慢"，而是**环在两次轮询之间积压 > 24 条**就溢出。
            #   ⇒ 每省一条命令就缩短一轮 ⇒ 直接减少丢条。而这条 `0x22` 是**纯冗余**：
            #     `sub=26` 的每条事件**都带 tick**（同一个设备时间源）。
            #   ★ 口径变化必须说清：`tickv` 从"**此刻**的 tick"变成"**最后一条事件**的 tick"
            #     ⇒ 它会**略旧**（落后一个环的积压，约 1 轮）。
            #     这**不影响**它的用途：`d_tick`（"板子间隔"）只用来回答"板子还在不在跑"，
            #     不是精确时戳源（精确时戳取自 WAVEBUF 里每条事件自带的 tick）。
            #   ★ 反向：`cnt == 0`（静止/无事件）时**仍读** ⇒ 静止态行为与以前完全一致
            #     （否则`d_tick` 会停在旧值，看起来像"板子卡了"）。
            if last_tk is not None:
                tickv = last_tk
            else:
                st_, pt_ = d.send(0x22, struct.pack("<IH", shm + 0x0008, 1), expect_len=None)
                if st_ == "ACK" and len(pt_) >= 4:
                    tickv = struct.unpack("<I", pt_[:4])[0]

            # ── ⑤ 已应用频率（权威来源 = sub=14；每 5 轮一次，它变得慢）──
            if n % 5 == 0:
                s4, p4 = d.send(0x39, struct.pack("<BBI", 19, 14, 0), expect_len=32)
                if s4 == "ACK" and len(p4) >= 24:
                    mosrc, cmd_n, ap_n, rej_n, ap_u, lim_rem = struct.unpack("<6I", p4[:24])
                    ap = float(ap_u)

            t = time.time()
            # ── 位置：取时间序列的最新点（= 板子当前拍的精确位置）──
            with LOCK:
                pos = WAVEBUF[-1][1] if WAVEBUF else 0.0
            revs = pos / pitch if pitch else 0.0
            # ── ★★ 速度：从**全分辨率时间序列**算（分子分母同为设备口径）──
            #    只取**相邻两点**（100 Hz ⇒ Δtick≈100）；Δtick 太大说明中间丢过，不算。
            vel = 0.0
            with LOCK:
                if len(WAVEBUF) >= 2:
                    (t1_, p1_), (t2_, p2_) = WAVEBUF[-2], WAVEBUF[-1]
                    dtk = (t2_ - t1_) & 0xFFFFFFFF
                    if 0 < dtk <= 500:
                        vel = (p2_ - p1_) / (dtk * TICK_US * 1e-6)
            d_tick = ((tickv - prev_tick) & 0xFFFFFFFF) if prev_tick is not None else 0
            prev_tick = tickv
            dt_s = d_tick * TICK_US * 1e-6
            pc_hz = 1.0 / max(1e-6, t - prev_wall)
            prev_wall = t
            # ★★★ HIL 反向通道：把 PC 累计的**绝对位置**写回板子的 `wire[50]`。
            #   位置选在这里（`pos` 已算完、还没进 STATE）⇒ 写的是**本轮的**位置。
            #   ★ 每 2 轮写一次: 一次 `0x21` 固定开销 ~10 ms，每轮写会明显拖慢轮询。
            if HIL_VIRT_POS_ON and shm and (n % 2 == 0):
                d.send(0x21, struct.pack("<II",
                       shm + OFF_WIRE + HIL_VIRT_POS_SLOT * 4,
                       struct.unpack("<I", struct.pack("<f", pos))[0]), expect_len=None)

            n += 1
            evt_n += cnt
            if t - t_rate >= 1.0:
                _dt = t - t_rate
                evt_rate = evt_n / _dt
                evt_n = 0
                # ★★ 结算通道直方图 ⇒ **产出分解表**（条/s，降序，只留前 12 条）
                #   ★ 口径说明：这里统计的是"**我读到的**"分布。环溢出时丢的条是**随机**的
                #     （消费者从 from_seq 顺序取，丢的是它没来得及取的尾部），
                #     所以"读到的分布"是"产出分布"的**无偏估计** ⇒ 可以据此定位大头。
                s_ch_top = sorted(
                    ({"ch": c, "name": bb_slot_name(c), "hz": v / _dt}
                     for c, v in s_ch_hist.items()),
                    key=lambda x: -x["hz"])[:12]
                s_ch_hist = {}
                t_rate = t

            # ── ⑥ ★★★ 管理面（下位机仪表盘）—— 低频，见 MGMT_AUTO_S 上方长注释 ──
            #   ★ 位置选在这里的理由: 本轮的运动面读已经结束（`sub=26`/`sub=14` 都发过了），
            #     所以管理面那 ~9 次往返**不会插在**"位置序列"的两次采样之间 ——
            #     否则会给 `WAVEBUF` 的 Δtick 加一个假的跳变（那会被当成"丢过点"）。
            #   ★ 失败**不许影响主通路**: 整段包在 try 里，错误只写进 `s_mgmt["err"]`。
            if MGMTQ:
                MGMTQ.pop(0)
                s_mgmt_t0 = 0.0      # 强制：`t - 0` 必然 >= MGMT_AUTO_S
            if (MGMT_AUTO_S > 0.0 or s_mgmt_t0 == 0.0) and (t - s_mgmt_t0) >= MGMT_AUTO_S:
                s_mgmt_t0 = t
                try:
                    mgmt_poll(d, s_mgmt)
                except Exception as ex:
                    s_mgmt["err"] = "%s: %s" % (type(ex).__name__, ex)

            # ★★ 「命令」按运动源自适应 —— 否则曲线是一条假的平线：
            #    程序面模式(mosrc=1) 命令 = WIRE[12]；脚手架模式 WIRE[12] 是**残留值、没人维护**
            #    （实测恒为 6.0 Hz），真正的命令是本进程下发的 req_hz。与 §〇 第 6 条同族。
            req = STATE.get("req_hz", 0.0)
            cmd = _f32(mirror[SLOT_WIRE0 + W_REQ_RATE])
            cmd_eff = cmd if mosrc == 1 else req
            mis = bool(req > 1.0 and abs(ap - req) > max(1.0, req * 0.05))

            # ══════════ ★★★ 层级对账（2026-09-22）══════════
            # `src_intent` = 上位机**意图**（/motion?src= 记下的）；`src_dev` = **板子实读**。
            # ★ 为什么读 `mirror[SLOT_WIRE0 + 10]`: 镜像槽 16..31 就是 `wire[0..15]`
            #   （打底时 `(OFF_WIRE, 16)` 决定），而 `wire[10]` 的掩码位（镜像槽 26）
            #   **没被 `DELTA_MASK_DEF_LO` 屏蔽** ⇒ 它是**免费可读**的，无需额外一次 0x22。
            src_dev = "program" if mosrc == 1 else "scaffold"
            src_intent = STATE.get("src_mode", "-")
            wire_ena = _f32(mirror[SLOT_WIRE0 + 10])
            src_match = (src_intent == "-") or (src_intent == src_dev)

            # ══════════ ★ 停机验证：点停之后**读回**并下结论 ══════════
            # ★ 判据（能失败）: 停机后 `ap_hz`（硬件实际在发的频率）必须归 0，
            #   且**保持**归 0（不是"闪一下 0 又回来"—— 用户遇到的正是后者）。
            # ⇒ 用**连续满足时长**判定，而不是"某一瞬间是 0"。
            # ★★ 反向判据: 若 `ap_hz` 在 3.5 s 后仍 ≥ 1 ⇒ `stop_ok=False`，
            #   并把**具体是哪一环没关**报出来（wire[10]/wire[12]/ap）。
            stop_t = STATE.get("stop_t", 0.0)
            stop_ok = STATE.get("stop_ok", None)
            stop_msg = STATE.get("stop_msg", "—")
            hold = STATE.get("_stop_hold", 0.0)
            if stop_t > 0.0:
                age = t - stop_t
                # 「安静」= 硬件没在发脉冲 **且**（程序面时）程序也没在请求
                quiet = (ap < 1.0) and (mosrc != 1 or abs(cmd) < 1.0)
                if quiet:
                    hold += max(0.0, t - STATE.get("_stop_prev", t))
                    if hold >= 1.0 and stop_ok is None:
                        stop_ok = True
                        stop_msg = "已停住（连续 %.1f s 无脉冲，命令 %.1f Hz / 程序 %s）" % (
                            hold, ap, ("%.0f Hz" % cmd) if mosrc == 1 else "无（脚手架）")
                else:
                    hold = 0.0
                    if stop_ok is True:
                        stop_ok = None      # ★ 又动了 ⇒ 撤回"已停住"（不许留下过期的绿）
                if age > 3.5 and stop_ok is None:
                    stop_ok = False
                    stop_msg = ("★ 停失败: ap=%.0f Hz · wire[10]=%.2f · wire[12]=%.0f Hz · 源=%s"
                                % (ap, wire_ena, cmd, src_dev))
            else:
                hold = 0.0

            with LOCK:
                _raw = _f32(mirror[SLOT_RAW])
                STATE.update(t=t, raw=_raw, deg=_deg_from_raw(_raw),
                             revs=revs, pos_mm=pos, vel_mm_s=vel,
                             cmd_hz=cmd, cmd_mm_s=cmd / STEPS_PER_REV * pitch,
                             cmd_eff_hz=cmd_eff,
                             cmd_eff_mm_s=cmd_eff / STEPS_PER_REV * pitch,
                             ap_hz=ap, ap_mm_s=ap / STEPS_PER_REV * pitch,
                             mosrc=src_dev,
                             src_dev=src_dev, src_intent=src_intent, src_match=src_match,
                             wire_ena=wire_ena, wire_req=cmd,
                             stop_ok=stop_ok, stop_msg=stop_msg,
                             _stop_hold=hold, _stop_prev=t,
                             cmd_n=cmd_n, applied_n=ap_n, rej_n=rej_n, lim_rem=lim_rem,
                             mismatch=mis,
                             tick=tickv, d_tick=d_tick, dt_s=dt_s,
                             run=runv, ov=ovv, n=n, ok=True, msg="实时",
                             src_rule="device_tick", rate=pc_hz,
                             evt_n=cnt, evt_rate=evt_rate, drop=drop_seen,
                             # ★ 产出分解（各通道 条/s）—— 用来回答"419 条/s 是谁贡献的"
                             ch_top=list(s_ch_top),
                             hil_virt_pos=(pos if HIL_VIRT_POS_ON else None),
                             wave_n=len(WAVEBUF),
                             # ★ 管理面快照 —— **拷贝一份**（`/state` 在另一个线程里序列化，
                             #   直接给 `s_mgmt` 会让它在 json.dumps 期间被 live 线程改）
                             mgmt={"items": list(s_mgmt.get("items", [])),
                                   "t": s_mgmt.get("t", 0.0),
                                   "err": s_mgmt.get("err"),
                                   "n_entries": (len(s_mgmt["manifest"])
                                                 if s_mgmt.get("manifest") else 0),
                                   # ★ 目录全表也带上 —— 它是"板子有哪些可读区"的**唯一真值源**
                                   #   （`manifest.h` 的纪律：PC 端不许另写一份地址）。
                                   #   带它还有一个实战用途: 对账失败时能立刻看出是
                                   #   "**板子自报的 words 与源码/工具不一致**"（固件比源码旧），
                                   #   而不是猜"是不是解析器坏了"。
                                   "dir": [{"n": x["name"], "a": x["addr"],
                                            "w": x["words"], "k": x["kind"],
                                            "f": x["flags"]}
                                           for x in (s_mgmt.get("manifest") or [])]})
            time.sleep(max(0.0, 1.0 / hz))
        except Exception as e:
            err += 1
            with LOCK:
                STATE.update(ok=False, msg="%s: %s" % (type(e).__name__, e))
            if err > 8:
                try:
                    d.close()
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    d = Dcl(port); shm = None; err = 0
                    have_mirror = False; from_seq = None; prev_tick = None
                except Exception:
                    time.sleep(1.0)
            time.sleep(0.1)

# ══════════════════ CSV 回放 ══════════════════
def find_col(hdr, *names):
    for i, h in enumerate(hdr):
        hs = h.strip().lower().replace(" ", "")
        for nm in names:
            if nm in hs:
                return i
    return None


def run_csv(path, pitch, speed):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        hdr = next(rd, [])
        rows = [r for r in rd if len(r) >= 6]
    i_tick = find_col(hdr, "tick") or 0
    i_raw = find_col(hdr, "sensor[0]", "raw")
    i_deg = find_col(hdr, "sensor[1]", "deg")
    i_cmd = find_col(hdr, "wire[12]")
    with LOCK:
        STATE.update(src="csv", msg="%s (%d 行)" % (os.path.basename(path), len(rows)),
                     pitch=pitch, ok=True)
    if i_raw is None:
        with LOCK:
            STATE.update(ok=False, msg="CSV 里找不到 SENSOR[0] 列；表头=%s" % hdr[:8])
        return
    uw = Unwrap()
    n = 0
    t0 = time.time()
    tick0 = float(rows[0][i_tick])
    while True:
        for r in rows:
            try:
                tick = float(r[i_tick])
                raw = float(r[i_raw])
                deg = float(r[i_deg]) if i_deg is not None else raw * 360.0 / COUNTS_PER_REV
                cmd = float(r[i_cmd]) if i_cmd is not None else 0.0
            except Exception:
                continue
            t_sim = (tick - tick0) / 10000.0            # 100 µs/拍
            uw.feed(raw)
            revs = uw.total / COUNTS_PER_REV
            n += 1
            # ★ 回放也填**全分辨率序列** ⇒ 同一个 `/wave` 端点在回放模式下照样能画真波形。
            #   这样"实时"与"回放"共用同一个渲染器与同一个计算口径（用户的判据）。
            with LOCK:
                WAVEBUF.append((int(tick), revs * pitch))
                if len(WAVEBUF) > WAVE_MAX:
                    del WAVEBUF[:len(WAVEBUF) - WAVE_MAX]
            prev = STATE.get("_pc", uw.total)
            d_cnt = uw.total - prev
            # ★ CSV 里的 tick 列**本来就是设备 tick** ⇒ 回放也能用设备口径算速度（与 live 一致）
            d_tick = int(tick - STATE.get("_ptick", tick))
            dt_s = d_tick * TICK_US * 1e-6
            with LOCK:
                STATE.update(t=t_sim, raw=raw, deg=deg, revs=revs, pos_mm=revs * pitch,
                             vel_mm_s=(d_cnt / COUNTS_PER_REV * pitch / dt_s) if dt_s > 1e-9 else 0.0,
                             cmd_hz=cmd, cmd_mm_s=cmd / STEPS_PER_REV * pitch,
                             cmd_eff_hz=cmd, cmd_eff_mm_s=cmd / STEPS_PER_REV * pitch,
                             ap_hz=0.0, ap_mm_s=0.0,
                             tick=int(tick), d_tick=d_tick, dt_s=dt_s,
                             rate=1.0 / dt_s if dt_s > 1e-9 else 0.0,
                             src_rule="device_tick(csv)",
                             mosrc="replay", mismatch=False,
                             evt_n=1, evt_rate=1.0 / dt_s if dt_s > 1e-9 else 0.0,
                             drop=0, wave_n=len(WAVEBUF),
                             n=n, ok=True, _pc=uw.total, _ptick=tick)
            # 按 speed 倍率对着墙钟放
            target = t0 + t_sim / max(0.01, speed)
            dtw = target - time.time()
            if dtw > 0:
                time.sleep(min(dtw, 0.2))
        t0 = time.time(); tick0 = float(rows[0][i_tick])


# ══════════════════ HTTP ══════════════════
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        """★ 唯一 HTTP 入口 —— **必须存在**。

        ★★★ 2026-09-22 事故（我踩的）：一次 Edit 把 `do_GET` 的方法头连带吃掉了，
        它原来的 body（`/wave` / `/state` / index.html 回退）被并进了 `_ctl`。
        ⇒ **磁盘上的文件里没有任何 HTTP 入口符号**，而 `py_compile` **完全无话可说**
           （语法合法、类合法、函数合法 —— 缺的是"被框架回调的名字"）。
        ⇒ 症状极具误导性: **跑着的旧进程一切正常**（/state 有数据），
           只有**新加的路由**（/stop）返回 index.html —— 看起来像"路由写错了"，
           实际是"**这份代码根本没上线**"。
        ⇒ 判据（能失败）: `do_GET` 必须存在，且 `_ctl` 必须从它可达。回归脚本里
           按**符号**检查（见 tools/hostsim/selftest_routes.py），不按"能跑起来"检查。
        ★ 与 §〇 第 6 条同族：**"文件改过了" ≠ "改动生效了"**。"""
        return self._ctl(self.path)

    def _ctl(self, path):
        q = {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
        if path.startswith("/pin"):
            CMDQ.append(("pin", q.get("sub", 0), q.get("arg", 0)))
            return self._json({"queued": "pin", **q})
        if path.startswith("/wire"):
            CMDQ.append(("wire", q.get("i", 0), q.get("v", 0)))
            return self._json({"queued": "wire", **q})
        if path.startswith("/stop"):
            # ★★★ **通用急停** —— 语义上必须**永远有效**，与当前运动源无关。
            #   缺陷（用户 2026-09-22 实测）：原来只有 `/motion?on=0`，而它走**脚手架路径**
            #   （`sub=1 0` + `sub=6`）⇒ **程序面模式下 `sub=1` 没人在听**，
            #   而 `sub=6` 只停了几拍，③层程序**每拍**又按误差把脉冲拉回来
            #   ⇒ 现象是"**停了又跑**" —— 比"完全没反应"更危险（人会以为停了）。
            #   ⇒ 正解: **两条路一起打**（幂等、互不干扰）:
            #       · `wire[10]=0` ⇒ 程序面的使能（程序读它）
            #       · `sub=1 arg=0` ⇒ 脚手架的频率 0
            #       · `sub=6`        ⇒ 安全态（停脉冲 + 按 stop_hold 决定静止态）
            #   ★ 顺序不能反: **先关两条路的使能，最后才 sub=6** —— 否则 `sub=6` 之后
            #     ③层程序会在下一拍重新拉起频率。
            CMDQ.append(("wire", 10, 0.0))     # 程序面使能 = 0
            CMDQ.append(("pin", 1, 0))         # 脚手架频率 = 0
            CMDQ.append(("pin", 6, 0))         # 安全态
            with LOCK:
                STATE.update(req_hz=0.0, lim_ms=0,
                             stop_t=time.time(), stop_ok=None,
                             stop_msg="停机中…（%s 模式下正在验证）" % STATE.get("mosrc", "?"),
                             _stop_hold=0.0)
            return self._json({
                "queued": "stop",
                "note": "两条路一起打（程序面 wire[10]=0 + 脚手架 sub=1/sub=6）",
                # ★★ 语义必须说清（这就是"层级"）:
                #   · **程序面模式下 `wire[10]` 是唯一的停机权威** —— 程序每拍跑，
                #     `sub=1`/`sub=6` 只是"外加一记"，会在 100 µs 内被程序面覆盖。
                #     ⇒ 少了 `wire[10]=0`，现象必然是"**停了又跑**"（用户实测）。
                #   · **脚手架模式下** `sub=1`（频率 0）+ `sub=6`（安全态）才是权威。
                #   · 所以"停"必须**两条都打** —— 这就是 `/stop` 存在的理由：
                #     **停止的语义不能依赖当前运动源**（否则人得先知道自己在哪条路上，
                #     而"人不知道 / 板子知道"正是这次事故的形态）。
                "verify": "3.5 s 内 /state 的 stop_ok 会给出结论（能失败）",
            })
        if path.startswith("/mgmtpoll"):
            # ★ 按需立刻跑一轮管理面（用户点"刷新"）—— 入队，由 live 线程执行（串口独占）。
            #   ★ 为什么不做成"HTTP 线程自己读串口": 串口是独占资源，两个句柄 = 已踩过的坑。
            MGMTQ.append(True)
            return self._json({
                "queued": "mgmtpoll",
                "note": "下一轮采集会读一遍管理面（0x64 目录 + 0x22 按名字读，约 90 ms）",
            })
        if path.startswith("/zero"):
            # ★★★ HIL 坐标原点（2026-09-22）—— 见 ZEROQ 上方长注释。
            #   ★ 为什么归零**不**是 UI 便利功能而是契约:
            #     PC 绝对位置的起点 = bridge 进程启动时刻，而③层程序的目标是**它自己的**
            #     坐标系。不对齐 ⇒ 误差恒定 ⇒ 满速跑（实测 1921 mm ⇒ 误差 −1800 ⇒ 3003 Hz）。
            #   ★ 它同时修掉了前端"行程归零"的**空操作**（原来只改本地 `zeroOffset`）。
            ZEROQ.append(True)
            return self._json({
                "queued": "zero",
                "note": "PC 绝对位置 → 0，并立刻把 wire[50]=0 写回板子（HIL 坐标原点对齐）",
            })
        if path.startswith("/motion"):
            on = q.get("on", "1") not in ("0", "false")
            A = float(q.get("A", 3000)); slope = float(q.get("slope", 30000))
            # ★★★ `src` = 运动源。**这是"层级"的显式协商**（用户 2026-09-22 指出"层级没处理好"）:
            #   板子上有**两条平行的运动通路**，而上位机原来**不知道自己在打哪条**:
            #     · `scaffold` = 脚手架直控: `sub=1/2/3/4` 直接下频率/方向/使能
            #     · `program`  = 程序面: ③层程序写 `wire[12..15]`; 上位机**只写程序的输入**
            #                    (本例: `wire[10]`=使能, `wire[50]`=虚拟绝对位置)
            #   ⇒ **两条路不能混用**，而且**停机必须两条都打**（见 `/stop`）。
            #   ★ 默认 scaffold 的理由: 它的生效条件**不依赖板上是否加载了程序**
            #     （裸板上 `program` 是**静默失效**的 —— ACK 了但没人消费，见 §「ACK≠生效」）。
            src = q.get("src", "scaffold")
            lim = int(q.get("lim", 30000))
            if on:
                if src == "program":
                    # 程序面: 只写**程序读的输入** + 确保源开关在程序面
                    CMDQ.append(("pin", 5, 1))         # ENA 极性（使能的前置）
                    CMDQ.append(("pin", 3, 1))         # 驱动器使能
                    CMDQ.append(("pin", 13, 1))        # ★ 运动源 = 程序面
                    CMDQ.append(("wire", 10, 1.0))     # ★ 程序使能（程序读 wire[10]）
                else:
                    # 脚手架直控: 声明极性 → 使能 → 限时 → 方向 → 选源 → 频率
                    CMDQ.append(("pin", 17, int(slope)))
                    CMDQ.append(("pin", 5, 1))
                    CMDQ.append(("pin", 3, 1))
                    CMDQ.append(("pin", 4, lim))
                    CMDQ.append(("pin", 2, int(q.get("dir", 0))))
                    CMDQ.append(("pin", 13, 0))
                    CMDQ.append(("pin", 1, int(A)))
            else:
                # ★ 停机走**通用急停**那条路（两条路一起打），不再只打脚手架
                CMDQ.append(("wire", 10, 0.0))
                CMDQ.append(("pin", 1, 0))
                CMDQ.append(("pin", 6, 0))
            with LOCK:
                if on:
                    # ★ 开跑要**清掉上一次的停机结论** —— 否则 HUD 会挂着一条过期的绿
                    #   （"已停住"），而机器正在转。**过期的绿比没有更危险**。
                    STATE.update(req_hz=A, src_mode=src, lim_ms=lim,
                                 stop_t=0.0, stop_ok=None, stop_msg="—",
                                 _stop_hold=0.0)
                else:
                    # ★ 与 `/stop` **同一套语义与同一套验证** ——
                    #   两条停机入口的差别只该在"谁来调"，不该在"停得干不干净"。
                    STATE.update(req_hz=0.0, src_mode=src, lim_ms=0,
                                 stop_t=time.time(), stop_ok=None,
                                 stop_msg="停机中…（%s 模式下正在验证）" % STATE.get("mosrc", "?"),
                                 _stop_hold=0.0)
            return self._json({"queued": "motion", "on": on, "A": A, "slope": slope,
                               "src": src, "lim_ms": (lim if on else 0)})
        if self.path.startswith("/wave"):
            # ★★★ 全分辨率位置序列（给"计算"用）—— 用户的判据：
            #   「演示可以用低分辨率，计算必须用全量数据」。
            #   `/state` 给**当前状态**（渲染用，60 fps 取最近值就够）；
            #   `/wave`  给**完整的 (tick, pos_mm) 序列**（实测 100 Hz）—— 波形/速度/判据都从这里算。
            #   ★ 两者都来自**同一份事件流**，所以不会互相矛盾。
            with LOCK:
                w = list(WAVEBUF)
            if not w:
                return self._json({"n": 0, "pts": [], "rule": "device_tick"})
            t0 = w[0][0]
            # 压缩成 [dt_tick, pos_mm] 对：dt_tick 相对首点，pos 保留 4 位小数。
            # ★ 为什么不直接发 tick：32 位 tick 的 JSON 文本比差值大 3 倍，
            #   而 900 点 × 2 个数在 12 s 窗口里差别不大 —— 但差值让前端少一次减法。
            pts = [[int(tk - t0), round(p, 4)] for tk, p in w]
            dts = [pts[i][0] - pts[i - 1][0] for i in range(1, len(pts))]
            # ══════════ ★★★ 点数率必须**用跨度算**，不能用中位间隔（2026-09-22 修正）══════════
            # 原 note 写「d_tick_med≈100 ⇒ 100 Hz」——**两处都过期了**：
            #   ① 实测（AS5600 绑定后）`d_tick_med=20` 拍，不是 100；
            #   ② ★ 更要命的是**口径**: `d_tick_med` 是**分位数**，而分位数**不免疫长尾**。
            #      实测同一份数据: 中位 20 拍 · **平均 50 拍**（45036 拍 / 900 点）
            #      ⇒ 用中位反推率会**高估 2.5 倍**（得 500 Hz，真值 ~200 Hz）。
            #   ⇒ 与 §〇 第 10 条同族: **极值/分位数不免疫采样拍频，累积量才免疫**。
            #      一个序列的"率"是**累积量口径**（点数 ÷ 总跨度），必须用两端点算。
            # ★ 所以对外**给算出来的率**，并把中位数只当"抖动指示器"。
            span_tick = (pts[-1][0] - pts[0][0]) if len(pts) > 1 else 0
            rate_pt = (len(pts) / (span_tick * TICK_US * 1e-6)) if span_tick > 0 else 0.0
            return self._json({
                "n": len(pts), "t0": int(t0), "pts": pts,
                "tick_us": TICK_US,
                "rule": "device_tick",           # ★ 时间轴是板子的拍计数，不是 PC 墙钟
                "d_tick_med": (sorted(dts)[len(dts) // 2] if dts else 0),
                "d_tick_avg": (span_tick / (len(pts) - 1)) if len(pts) > 1 else 0.0,
                "span_tick": span_tick,
                "rate_pt": rate_pt,              # ★ 权威点数率（累积量口径）
                "note": ("每点 = 一次编码器 raw 变化（非抽样、无损）。"
                         "★ 轴的**真实点数率看 `rate_pt`**，**不要**用 `d_tick_med` 反推 ——"
                         "分位数不免疫长尾（实测中位 20 拍 / 平均 50 拍 ⇒ 中位高估 2.5 倍）。"
                         "`rate_pt` 若明显低于编码器更新率 ⇒ 中间丢过点。"),
            })
        if self.path.startswith("/state"):
            with LOCK:
                body = json.dumps({k: v for k, v in STATE.items() if not k.startswith("_")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        p = os.path.join(HERE, "index.html")
        if not os.path.isfile(p):
            self.send_error(404, "index.html missing"); return
        with open(p, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", metavar="COM")
    ap.add_argument("--csv", metavar="FILE")
    ap.add_argument("--pitch", type=float, default=8.0, help="丝杆导程 mm/rev（T8=8, T5=2）")
    ap.add_argument("--hz", type=float, default=200.0,
                    help="实时轮询率上限。★ 实测（2026-09-22）: "
                         "轮询率**不是**由它决定，而是由协议往返时间决定 —— "
                         "每轮要发 ~5 条命令（sub=26 / tick / sub=14 / 0x38 / HIL 写），"
                         "而每条有 ~10 ms 固定往返开销 ⇒ 单轮地板 ≈ 60 ms ⇒ **上限 ≈ 16 Hz**。"
                         "`1/--hz` 是**额外**等待 ⇒ 默认 25 时（sleep 40 ms）实际只有 9.8 Hz，"
                         "那 40 ms 是**纯浪费**。取 200（sleep 5 ms）把轮询率交给协议去定。"
                         "★ 实测（2026-09-22，同一条命令只差本参数，n=16）: "
                         "**9.8 → 13.8 Hz（+41%）**，而**再往上提 --hz 无效** —— "
                         "因为瓶颈已从 sleep 变成**协议地板**（每轮 ~5 条命令 × ~10 ms "
                         "固定往返 ⇒ 单轮 ≥ 50 ms）。"
                         "⇒ **这条实验本身就是「提升反馈频率」的上限证据**。"
                         "★ rate 抖动大（11~21 Hz）是正常的: `sub=26` 的应答长度"
                         "随事件数变（16+16n 字节），运动越快往返越久。")
    ap.add_argument("--speed", type=float, default=1.0, help="CSV 回放倍率")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if not a.live and not a.csv:
        a.csv = None
        print("★ 必须给 --live COMxx 或 --csv <文件>")
        return 2
    def _wrap(fn, fargs):
        """★ 采集线程必须**把死因写进 STATE**。

        否则线程死了、HTTP 还在服务，前端一直看到初始值 `未启动` —— 看起来像
        "还没连上"，实际是"已经崩了"，而且没有任何线索。2026-09-22 实测撞到：
        端口被另一个 bridge 实例占着 ⇒ 线程在 `Dcl(port)` 就抛了，
        而 /state 返回 `ok=false, msg=未启动` —— 我因此白查了一轮。
        ⇒ 与 §〇 第 8 条同族：**失败必须响亮**。"""
        try:
            fn(*fargs)
        except Exception as e:
            import traceback
            with LOCK:
                STATE.update(ok=False, src="dead",
                             msg="采集线程已退出: %s: %s" % (type(e).__name__, e))
            traceback.print_exc()

    th = threading.Thread(target=_wrap,
                          args=(run_csv, (a.csv, a.pitch, a.speed)) if a.csv
                          else (run_live, (a.live, a.pitch, a.hz)),
                          daemon=True)
    th.start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print("=" * 66)
    print("  运动可视化上位机  —  http://127.0.0.1:%d" % a.port)
    print("  数据源 : %s   丝杆导程 : %.2f mm/rev" % (a.csv or a.live, a.pitch))
    print("  Ctrl-C 退出")
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
