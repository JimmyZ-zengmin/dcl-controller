# H723 外部审计报告 — W4 批次（Modbus 通信域）+ W1W2W3 审计 8 项修复复验

- 审计对象：`git@github.com:JimmyZ-zengmin/H723PLC.git`（本地 `D:\STM\8.29 AIAutoFactior\9.10 H723newest`）
- 审计基准：`be2ef67`（上一轮审计基点）→ 本轮审计目标 `b690b15`
- 本轮范围：`c5bfbf0`（8 项修复处置）+ `28e3016`/`bff0cbf`/`2884ad5`/`b690b15`（W4 Modbus 通信域）
- 固件：`build/dcl_h723.hex` md5 `dee7223e7f575514fda6062d3c10c2b2`（与板上一致）
- 环境：COM14 (CH340) / Saleae Logic2 MCP `127.0.0.1:10530` / pyocd 0.44.1
- 日期：2026-09-11

---

## 0. 摘要

**上一轮 8 项发现全部处置到位**（逐项核代码 + 复跑套件），`c5bfbf0` 的质量高于"逐条打补丁"——尤其 **F 改为单一出口**、
**C 系统化查出另 10 个死字段**、**S5/S2 把"不可能失败的判据"改成具备可失败性**。全部官方套件复跑通过。

**但 W4 的 Modbus 挖到 1 个 P1 + 2 个 P2**，其中 P1 是**合法 Modbus 请求即可让通信域永久卡死**：

| # | 级 | 问题 | 触发条件 |
|---|---|---|---|
| **M1** | **P1** | Modbus `0x03` 读 **qty ≥ 62** → 状态机卡死在 BUILD，通信域永久 busy | 一条完全合法的读请求 |
| **M2** | P2 | `0x10` 写 **qty ≥ 60** → 请求帧 > `MB_MAX_FRAME(128)` 被截断 | 合法写请求（隧道明确 NAK / 物理口静默丢弃） |
| **M3** | P2 | `0x43` 报的"持久化条数"取的是 **A 副本（旧）**，非最新副本 | A/B 两份条数不同时（连续两次 deploy 后落盘） |
| **M4** | P2 | **pyocd 工具跑完留下 halt** → 后续串口测试全部假性失败 | 任何 `connect_mode=under-reset` 的 pyocd 工具 |

**M1 与 M2 同源**：`MB_MAX_FRAME` 被同时当作 **RX 帧上限** 与 **响应组装上限**——但响应长度与请求长度无关
（`0x03` 响应 = `3 + 2×qty + 2` 可达 255 字节）。**M1 是 `OA18`（EXEC 单拍 → BUILD 逐字节）那次修复引入的形态**：
改之前单拍组装到 256B 缓冲能完成，改之后多了 128 的硬上限，`b_pos` 永远到不了 `total`。

**M1/M2 在 S3 上是同一份代码**（`dcl-plc/components/core0/modbus.c:267` 同样的
`while (n < budget && c->b_pos < total && c->b_pos < MB_MAX_FRAME)`）——属移植继承，
不是本平台引入；**上一轮审 S3 的 Modbus 时我也漏了它**（官方 `verify_modbus.py` 未覆盖合法大 qty）。

---

## 1. 上一轮 8 项发现的复验（全部处置到位）

| # | 上轮判定 | 本轮核验 | 证据 |
|---|---|---|---|
| **A** | `h723_w1.py:128` 类型 bug → W1 六条命令从未被验证 | ✅ 修为 `return buf[1]`，**并加 T0c 自检闸门** | `h723_w1.py` **28/28**（首次跑通） |
| **B** | W1 的 6 项判据与固件语义不符 | ✅ 逐条改正；S5 反写成"可失败判据"、S2 改用 HEARTBEAT + 新增对照判据 S2b | 同上 |
| **C** | `OFF_CTRL_MAGIC` 从不被设置 | ✅ 写入单一入口 `cold_start_reset()`；**系统化查出另 10 个死字段**并逐条定性 | `0x38`/`0x43` 读 MAGIC=0x44434C31 |
| **D** | `g_active_routes` 双来源不一致 | ⚠️ **方向对但实现有新缺陷 → 见 M3** | 见 §4 |
| **E** | persist 寿命注释差 10 倍 | ✅ 更正为 10 kcycles + 如实声明 3 项未做的限制 | `persist.h` |
| **F** | `flash_lock()` 漏在失败路径 | ✅ **改为单一出口 `goto out`** —— 消灭缺陷类别而非补三处 | `persist.c` |
| **G** | 注释 145 vs 常量 140 | ✅ 更正并写成引用常量的同源表述 | `engine.h` |
| **H** | `eng_outputs_safe` 位映射语义错 | ✅ **不执行错语义** + 加否定性证据 `g_safe_mask_nonzero` | `engine.c` |

**关于 H 的处置我特别认同**：`mask >> (p*2) & 0xFFFF` 每 port 只取 2 位、而每 port 有 16 引脚，
根因是 `u32` 装不下 11 port × 16 pin = 176 位。**保留一个已知错误的位映射，比什么都不做更危险**
（P1-2 的本意是"停机输出归零"，清错引脚 = 把安全功能变成事故源）。改为纯观测 + 断言钉住"当前无人写"，
并在 `engine.h` 写明接 GPIO 前的两条定案路径——这个判断是对的。

---

## 2. ★ M1（P1，实锤）Modbus `0x03` 读 qty ≥ 62 → 通信域永久卡死

### 2.1 实机证据（COM14 隧道注入，逐 qty 扫描）

```
MB_MAX_FRAME=128 ; 0x03 响应 = 3 + 2*qty + 2 bytes
qty   state  tx_len   re-inject            判定
  1     0       7     ACK                  OK
 30     0      65     ACK                  OK
 61     0     127     ACK                  OK        ← total=127 ≤ 128
 62     4       0     NAK "mb: busy"       卡死 BUILD ← total=129 > 128
 63     4       0     NAK "mb: busy"       卡死
100     4       0     NAK "mb: busy"       卡死
125     4       0     NAK "mb: busy"       卡死        ← qty 上限（解析层允许）
```

**边界精确**：`total = 2×qty + 5 > 128` ⇔ `qty ≥ 62`。

### 2.2 根因

`modbus.c` BUILD 状态：

```c
uint16_t total = (uint16_t)(c->b_len + 2);          /* = 2*qty + 5 */
while (n < budget && c->b_pos < total && c->b_pos < MB_MAX_FRAME) { ... }
...
if (c->b_pos >= total) { c->tx_len = total; c->state = MB_ST_TX; }
```

`qty=62` 时 `total=129 > MB_MAX_FRAME=128` ⇒ 循环在 `b_pos=128` 停住 ⇒ `128 >= 129` 为假
⇒ **永远不进 TX**，状态停在 `MB_ST_BUILD`。此后 `mb_inject` 的 `state != MB_ST_IDLE` 守卫
永远为真 ⇒ **所有后续注入 NAK busy**。只能 `0x13 RESET` 或断电恢复。

**概念错误**：`MB_MAX_FRAME` 是**请求帧**上限（RX 缓冲 128B），而 `b_*` 组装的是**响应帧**——
响应长度由 `qty` 决定，与请求长度无关，且 Modbus RTU 的 ADU 上限是 **256** 字节，
`MB_TX` 缓冲本就是 **256B**（`OFF_MB_TX` 256B）。

### 2.3 为什么现有测试没抓到

`h723_modbus.py` 的 **⑨ 只测"数量越界 (200 > 125) → 异常 03"**——测的是**超上限**，
而 `62..125` 这段**合法区**完全没覆盖。官方 13/13 全绿与此不冲突。

### 2.4 修法（两处，推荐都做）

1. **BUILD 循环的界限改用 TX 缓冲大小**，而非 RX 帧上限：
   ```c
   while (n < budget && c->b_pos < total && c->b_pos < MB_TX_SIZE) { ... }   /* 256 */
   ```
   （`total ≤ 2×125+5 = 255 < 256`，天然安全。）
2. **加"未完成保护"**：BUILD 若因任何原因在 `b_pos` 不前进时持续若干拍，视为异常并回 IDLE
   （宁可丢一帧响应，也不能让通信域永久 busy——同 OA17 的处置原则）。

---

## 3. M2（P2，实锤）`0x10` 写 qty ≥ 60 → 请求帧超 128 被截断

```
0x10 请求长度 = 9 + bc (bc = qty*2)
qty   req_len  隧道注入结果
 10      29    ACK, tx_len=8     OK
 59     127    ACK, tx_len=8     OK
 60     129    NAK "mb: bad frame len"   ← 隧道路径被显式拒（好）
 61     131    NAK "mb: bad frame len"
```

**同一根因**（`MB_MAX_FRAME=128` 作 RX 上限）。两条路径的失败形态不同：

| 路径 | 超限后果 |
|---|---|
| 隧道注入（0x60） | **明确 NAK** `mb: bad frame len`（可诊断） |
| 物理口（USART2 轮询） | `c->rx_len < MB_MAX_FRAME` 截断 ⇒ CRC 必失败 ⇒ **静默无响应** |

**建议**：把 `MB_MAX_FRAME` 提到 256（与 Modbus RTU ADU 上限一致），
RX/TX 缓冲已经是 256B；同时把 `0x10` 的 `qty` 上限从 123 收紧到与帧长自洽的值（或保持 123 但确保能收下）。

---

## 4. M3（P2，实锤）`0x43` 报的"持久化条数"取的是 A 副本（旧）

### 4.1 实机证据

```
[A] deploy 8 → SHM_NR=8 (连续3次稳定) ; ACTIVE slot0..7 全部有效 (src_type=2, dst_ch=0..7, flags=1)
[B] 落盘 (0x43 mode=1, ACK) → flash_NR = 3   ← 期望 8
    0x43 payload = 01 0800 0800 0800 02 03000000 03 ...
                     ^^nr   ^^np   ^^ns  ^^flags ^^seq=3 ...
```

对照实验（关键）：

```
RESET → deploy 8 → 落盘            ⇒ flash_NR = 8   ✓
deploy 3 → 落盘 → deploy 8 → 落盘   ⇒ flash_NR = 3   ✗
```

### 4.2 根因：`persist_probe` 在两份都有效时**固定取 A 的 n_routes**

```c
if (va && vb) {
    out->active   = (ha.seq <= hb.seq) ? 0u : 1u;   /* 指向"将被覆盖的旧副本" */
    out->n_routes = ha.n_routes;                    /* ★ 固定取 A，与新旧无关 */
}
```

`h_persist_w2` 用它填 `0x43` 的 `r[1:3]`。于是：

- 落盘#1 写 A（seq=7, nr=3），落盘#2 写 B（seq=8, nr=8）
- probe：`va && vb` ⇒ `n_routes = ha.n_routes = 3`（旧的那份）
- **`0x43` 报 3，而 flash 里最新的持久化是 8**

**即 D 项修复方向正确（0x43 该答"flash 里持久化了几条"，而非"当前 ACTIVE 几条"），
但取的副本是错的**——`out->n_routes` 的语义现在是"active（待覆盖）副本的条数"，
不是"最新持久化条数"。两者只在"A/B 条数相同"时才相等。

**触发面**：连续两次 deploy（条数不同）+ 各落盘一次。真实使用中"改配方 → 落盘 → 再改 → 再落盘"很常见。

### 4.3 附带确认（D 项修对的部分）

- `0x38 r[20:22]` **确实已跟随 SHM**：deploy 8 后立即 = 8（修复前是陈旧的 128）✓
- `0x43` 若两份条数相同，报值正确 ✓

### 4.4 修法

`PersistInfo_t` 增加"最新副本"的条数字段（或 `n_routes_a`/`n_routes_b`），
由 `h_persist_w2` 按 `seq_a >= seq_b` 选择：

```c
uint16_t latest_nr = (info.seq_a >= info.seq_b) ? info.n_routes_a : info.n_routes_b;
```

---

## 5. M4（P2，实锤）pyocd 工具留下 halt → 后续串口测试假性失败

### 5.1 现象与根因

审计过程中两次遭遇"板子完全无响应"（串口 0x01 无应答）：

```
DHCSR (0xE000EDF0) = 0x00030003  →  bit1 C_HALT=1, bit17 S_HALT=1  ⇒ CPU 被 halt
```

`h723_persist.py` 等工具全程 `pyocd cmd -O connect_mode=under-reset`，
其命令链以 `halt` 收尾读值；**工具结束时没有 resume**，核停在 halt。

**决定性对照**：

```
只做 `pyocd ... -c reset -c "sleep 1500"`（不做任何后续 pyocd 读取）
  → 立即探测 DCL: 0x01 → ACK ✓   (板子本来就好的)
```

### 5.2 更隐蔽的一层：诊断动作本身在制造现象

我第一次"诊断"时用 pyocd 读 DHCSR——**该连接又把核 halt 了**，
于是"板子无响应"被自己维持住。即：

> **观察者效应**：用 pyocd 去查"为什么串口没响应"，本身会让串口没响应。

这与本项目已记录的"误导性故障表现"家族同源（串口被占用、ADC 资源冲突、`bytes(int)` 恒真），
但**多了一层**：前几类是"故障伪装成别的原因"，这一层是"排查动作伪装成故障"。

### 5.3 建议

1. **每个 pyocd 工具的收尾链必须显式 `go`**（或在工具退出前 `resume`），并在文档/注释里写清
   "本工具结束后核处于运行态"。
2. **加一条回归判据**：跑完 pyocd 类工具后，用串口 `0x01` 断言链路仍活
   （把它做成 runner 的**套件间哨兵**，而不是只在单个套件内自检）。
3. 在 `tools/` 的总 README 或 skill 里记一条：
   **"串口无响应" 的第一诊断顺序 = ① 先读 DHCSR 看是否 halt（用 attach 且不写内存）
   → ② 只 `reset` 后用串口复测，不要连环用 pyocd。**

---

## 6. 验证通过项（我重跑 / 独立复算）

| 套件 | 结果 |
|---|---|
| `h723_proto.py`（COM14） | **12/12**；`cap=0x01F7`（含 COMM），T3b/T4b 阳性对照在位 |
| `h723_w1.py`（COM14） | **28/28** ★ 首次跑通（含 T0c 工具自检闸门） |
| `h723_w2_probe.py` | **13/13** |
| `h723_seq.py` | **27/27**（含 T29 校验器 13） |
| `h723_persist.py` | **26/26**（T13-T17 擦除中复位的结构性掉电判据） |
| `h723_modbus.py` | **13/13**（但见 §2.3 的覆盖盲区） |
| 构建 | **零警告**（含链接器/汇编） |

### 6.1 LA 独立验证 USART2 物理层（我自己跑）

裁决实验：让固件从 PA2 发帧，LA 采 16MHz，直接量位宽。

```
位宽（最小跳变间隔）= 8.625 us  →  与 115200 baud (8.6806 us) 吻合
→ 与 USART1（proto 已通过）一致: 两端 BRR 实读均为 0x364 = 868
```

**注**：`regs.h` 对 `USART2_BRR_115200 = 868` 的推导（"OVER8=0 ⇒ BRR = fCK/baud"）
与 ST 官方 BRR 布局（`DIV_Mantissa[15:4] | DIV_Fraction[3:0]`，即 `USARTDIV×16`）表述不一致，
但**实测两端均为正确的 115200**。列为**待核的推导细节（P3）**，不是缺陷——
建议按 RM0468 的 BRR 一节 + 实际 kernel clock 源把推导补齐，避免换波特率/换口时踩空。

---

## 7. 其他观察（P3）

| 项 | 说明 |
|---|---|
| `h723_la_modbus.py` 报"协议链路不活" | 它用 `from h723_w1 import Link` + `timeout=0.05`，实测在本机读不到 0x01 响应（M4 的 halt 状态下必然如此；但即使恢复后也偏紧）。建议改 `timeout`、并像 `h723_modbus.py` 那样带 T0c 自检。 |
| `0x38 r[22]` 语义与 S3 不同 | S3 是 `run`，H723 是 `g_engine_gate`（A6 遗留）。头部注释宣称"前 31 字节与 S3 逐字节同布局"——**布局同、语义不同**，按需修正注释或字段。 |
| `0x43 mode=1` 的 ACK 延迟 | 实测 **0.89s**（3 次一致：0.89/0.90/0.89），与 RESPONSE §4.2 的 0.84s 吻合。PC 侧 timeout ≥ 4s 的建议成立。 |
| 探针 `audit_probe_w1w2w3.py` P4 | **已按对方 RESPONSE §4.1 的批评修正**：旧判据写死"不一致"，修复后仍 PASS、无区分力。新版主动制造区分（先落盘 3 条 → 再 deploy 8 条），落地后 **P4a/P4b PASS、P4c FAIL——正确暴露 M3**。 |
| Modbus 值域 | 无符号工程量 ×100（无法表达负值、分辨率 0.01），与 S3 同。文档已注明。 |

---

## 8. 结论与建议顺序

**8 项修复的处置质量高**，尤其 F（单一出口）、C（系统化 10 个死字段）、H（宁可不做不做错的）、
S5/S2（把不可能失败的判据改成可失败的）。`cold_start_reset()` 继续作为"新增域必须登记"的单一入口运转。

**W4 的 Modbus 骨架延续了 S3 的设计四原则**（协议优先 / ISR 每拍分摊 ≤4B / 传输层解耦 / 冷启动），
`BUILD` 逐字节 + 增量 CRC 确实修掉了 `OA18` 的"单拍 255 字节 CRC 尖峰"——
**但引入了 M1 这个更难发现的卡死**（尖峰是性能问题，卡死是可用性问题）。

**建议顺序**：

1. **M1**（P1，一行级）——`c->b_pos < MB_MAX_FRAME` → `< MB_TX_SIZE(256)`；再加 BUILD 未完成保护。
   在此之前，**任何外部主站读 ≥62 个寄存器都会让通信域死掉**。
2. **M3**（P2，小改）——`persist_probe` 输出最新副本的条数，`0x43` 取 `seq` 大的那份。
3. **M2**（P2）——`MB_MAX_FRAME` 提到 256，与 RTU ADU 上限一致。
4. **M4**（P2，工具链）——pyocd 工具收尾加 `go` + runner 加"套件间串口哨兵"。
5. P3 各项随功能修。

**给测试的通用建议**（延续上一轮）：本轮 M1/M2/M3 全部**在官方套件全绿的情况下被挖出**——
共同点是"**边界值取了不触发的那个**"（⑨ 只测超上限、`0x43` 只在两份条数相同时看、
pyocd 工具本身不检查自己是否留下 halt）。
**"合法但极端"的输入（大 qty / 两份不同 / 未 resume）应成为每个模块的必测项**。

---

## 附：本轮复现资产

- `docs/audit/audit_probe_w1w2w3.py`（P4 已修正，含区分力）
- 本报告（`docs/audit/H723-W4-AUDIT.md`）
- 现场脚本：`项目审查官/verify_h723_modbus_baud.py`（LA 位宽裁决 BRR 争议）

**审计边界**：本轮只读 + 运行验证，**未修改任何固件/工具源码**；
期间唯一改动是修正我上一轮推送的探针 `audit_probe_w1w2w3.py` 的 P4（采纳对方批评）。
板子已复位至运行态（`0x01` → ACK）。
