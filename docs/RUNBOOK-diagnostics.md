# 排障手册 · 管理面（RUNBOOK-diagnostics）

> 2026-09-13 起，本项目的排障方式是：**用常规非阻断通信问板子**，不 halt、不停引擎、不开调试器。
> 入口只有一个：`tools/mgmt.py`。
> 背景与设计理由见 `docs/AUDIT-faultsuite.md`、`src/manifest.h`。

---

## 0. 三条硬规矩（先立规矩，再谈命令）

**规矩一：能走协议就别走调试器。**
调试器会停核 —— 一停，拍中断不跑、引擎停摆、DWT 计时冻结。
**观测不得改变被测对象**（本项目的"铁律 0"）。
只有在协议**表达不了**的时候才用调试器，且用完必须 `go` 放核。

**规矩二：地址一律来自板子自报的目录（`0x64`）。**
PC 侧**不许硬编码地址或偏移**。实测代价：硬编码 `g_shm=0x200084A0`，
后来加一个全局就把它挪成了 `0x200084C0`，于是读台账读出 `magic=0` 的**假故障**，白查一轮。

**规矩三：判读写进工具，不靠记忆。**
`--symptom <症状>` 会按预设顺序读该读的区并给出结论。
诊断知识落点在代码（`tools/mgmt.py` 的 `plan` 表）+ 本文档，不在人脑里。

---

## 1. 三个起步命令

```bash
python tools/mgmt.py --manifest        # 板子自报有哪些诊断区 (名字/地址/解释方式)
python tools/mgmt.py --health          # 一次拿全套健检结论  ← **出问题先跑这个**
python tools/mgmt.py --symptom comm    # 按症状取"该读什么" (comm|reset|faults|engine)
python tools/mgmt.py --read FAULTLOG   # 按名字读一个区, 按 kind 解释
python tools/mgmt.py --watch 5         # 连测 5 轮, 看哪些量在动/不动
python tools/mgmt.py --boot            # 复位归因 (为什么复位了)
```
退出码：`0` 健康 / `1` 有结论判为异常 / `2` 通道读不到（**先查链路，别查固件**）。

---

## 2. 症状 → 读什么 → 判据 → 处置

| 症状 | 先读（按顺序） | 判据（能失败的） | 结论/处置 |
|---|---|---|---|
| **协议口一个字都不回** | ① `--manifest` ② 链路质量（见 §3） | 成功率应 ≈100% | 先量链路质量。**偏低 = 接触不良**，不是固件 |
| **偶发无响应 / 时好时坏** | 链路质量 + `MB_DIAG.bytes` | 见 §3；`bytes` 是否随发送增长 | 接触不良 / 引脚松动；`bytes` 不动 = 信号没到 PD6 |
| **收不到但发得出** | `MB_DIAG`（bytes/maxrx/short/erracc/ERRCLR/FIFO） | `CR1 bit29(FIFO)` 必须为 1；`bytes` 应随对端发送增长 | FIFO 关 ⇒ 100µs 拍撑不住 86.8µs 字节间隔；`bytes=0` 且 `erracc` 不动 ⇒ 信号没到 |
| **收到一点就没了** | `MB_DIAG.erracc` + `ERRCLR`；`FAULTLOG` | `ORE=1` 且 `ERRCLR` 不再增长 | **接收自锁死**（ORE 未清）⇒ 见 `docs/audit/H723-485-RX-AUDIT.md` |
| **帧被截断 / 大帧慢** | `MB_DIAG[23..26]`（板内响应延迟，单位 100µs 拍）、`[28]FASTOK`、`[29]RX_FULL` | `FASTOK` 应 ≈ 帧数；`RX_FULL` 应 0 | `FASTOK` 不涨 ⇒ 早判帧没生效；`RX_FULL` 涨 ⇒ 帧间无静默（对端违反 Modbus 3.5 字符） |
| **板子在反复复位** | `--boot` 读两次 | `启动次数` 必须**不涨** | 在涨 ⇒ 有东西在复位它；看复位原因位（**若多位同时置位，工具会拒绝下结论** —— 那时的真问题是 RSR 未清或位定义不符） |
| **怀疑固件跑飞/卡死** | `--health` 的 HEARTBEAT | 两次读必须**变大** | 不动 ⇒ ISR 没跑；变大但引擎不动 ⇒ `ENGINE_RUN=0`（正常停机） |
| **计时数字全是 0** | `FAULTLOG` 找 `TIMEBASE` | `TIMEBASE>0` ⇒ 时基被停 | 多半是调试器会话把 `DWT_CYCCNT` 停了 ⇒ **复位即恢复**；此时所有 DWT 计时统计不可用 |
| **故障在哪、哪类、什么时候** | `FAULTLOG`（首例/末例/24 类计数）；SD 日志头快照 | `total == Σcats` 必须成立 | 不成立 ⇒ 台账自己坏了（部分写入/重入/布局漂移） |
| **引擎行为不对** | `SHM_CTRL`（HEARTBEAT/ENGINE_RUN/N_ROUTES）、`FAULTLOG`（ISR_OVER/SCAN_DIV0） | ISR_OVER 涨 ⇒ 拍超预算 | 先看是不是加了热路径成本；SCAN_DIV0 涨且 TIMEBASE 也在涨 ⇒ 时基问题 |
| **Modbus 不响应** | `MB_CTRL`（state/enabled/frames_rx/tx/err_crc/err_exc）、`MB_RX`（原始字节） | `enabled=1`、`state` 必须能回到 IDLE | 死在某状态 = 锁死；`MB_RX` 里能看到对端到底发了什么（**最硬的证据**） |

---

## 3. 链路质量：先量"成功率"，别用"一次成功"当证据

```bash
# 发 N 条最小命令, 数成功率 (健康应 ≈100%)
python - <<'PY'
import serial, time
def crc(d):
    c=0xFFFF
    for b in d:
        c^=b<<8
        for _ in range(8):
            c=((c<<1)^0x1021)&0xFFFF if c&0x8000 else (c<<1)&0xFFFF
    return c
def fr(c,p=b''):
    b=bytes([c,len(p)&0xFF,len(p)>>8])+p; x=crc(b)
    return bytes([0xC0])+b+bytes([x&0xFF,x>>8])
s=serial.Serial('COM14',115200,timeout=0.05); ok=0; N=60
for _ in range(N):
    s.reset_input_buffer(); s.write(fr(0x01)); s.flush()
    t0=time.time(); buf=bytearray()
    while time.time()-t0<0.25:
        d=s.read(64)
        if d: buf+=d
        if len(buf)>=10: break
    if len(buf)>=10 and buf[0]==0xC1:
        ln=buf[2]|(buf[3]<<8)
        if crc(buf[1:4+ln])==(buf[4+ln]|(buf[5+ln]<<8)): ok+=1
print('链路质量 %d/%d = %.0f%%'%(ok,N,100.0*ok/N))
PY
```

★ **一次成功不构成证据**（本手册成文当天就栽在这：12 条里只回 1 条 —— 8% —— 却先宣布"通了"；
真因是**一根线接触不良**）。**成功率**才是判据。
★ 分辨方向：**板→PC** 用最小固件 `DCL_MIN_UART=1`（每秒一行 `HB`）；
**PC→板** 用上面的成功率。
两个方向要分别证 —— 只有一向通时，"能打印但不应答"与"固件坏了"看起来一样。

---

## 4. 与看门狗的关系（为什么先做这个）

看门狗要回答的第一个问题是"**它为什么复位了**"。那需要：
- `BOOT_AXI`（启动次数 + RCC_RSR + RCC_BDCR）—— **在 AXI，本次之前只能靠调试器读**；
- 复位之后**不需要调试器**就能读数 ⇒ 本手册的只读 AXI 窗口 + 自报目录就是它的前置。

⚠️ 已知未决项（看门狗开工前要处理的）：`BOOT_AXI` 的 `RCC_RSR` 读出 `0x01FA0000`
—— 同时置了 5 个"复位原因"位且有未定义位。物理上一次复位只能有一个主因，
⇒ 要么 **RSR 没被清（RMVF）**、要么**位定义与实际不符**。工具现在会拒绝照抄"多个原因"，
但**根因未查**。做看门狗时会直接依赖这个判据，必须先查清。
