# MEMORY-LAYOUT — 内存布局与数据放置 (2026-09-12)

> 回答三个问题：**搬进的数据放哪里、计算完成的数据放哪里、shadow 放哪里**，
> 以及"太松散浪费 / 太紧凑被覆盖"的分析。所有数字引自 `build/dcl_h723.map` 与 `src/engine.h`。

---

## 1. DTCM 总地图 (128KB: 0x20000000 ~ 0x20020000)

| 区段 | 地址范围 | 大小 | 说明 |
|---|---|---|---|
| .data | 0x20000000 ~ 0x2000002C | 44 B | 已初始化全局 |
| .bss | 0x20000040 ~ 0x20008218 | 32.8 KB | 零初始化全局（含引擎观测面）|
| **SHM** | **0x20008220 ~ 0x20010220** | **32 KB** | 数据总线（见 §2）|
| **空闲** | 0x20010220 ~ 0x20020000 | **63.5 KB** | 未用（栈从顶向下，当前仅占 ~160B）|

**结论：DTCM 余量 63.5KB —— "浪费内存"在本平台不成立，真正的约束是 §2 的分区边界。**

---

## 2. SHM 分区表 (32KB: 偏移 0x0000 ~ 0x8000)

| 偏移 | 区 | 大小 | 内容 / 写者 |
|---|---|---|---|
| 0x0000 | **CTRL 控制块** | 0x40 | MAGIC/VERSION/**HEARTBEAT(0x08)**/RELOAD(0x0C)/**ENGINE_RUN(0x0D)**/N_ROUTES/PROG_MAGIC/**GPIO_MASK(0x34)**/N_SEQ —— 写者=协议命令+ISR |
| 0x0040 | **SENSOR_MAP** | 0x100 (64×f32) | **① 搬进的数据**：DI[3..6]/AI[8..10]/HIL 反馈[2]，写者=di_poll/adc_poll |
| 0x0140 | **ACTUATOR_STATUS** | 0x100 (64×f32) | **② 计算完成的数据**（执行器命令）：写者=扫描/seq；DO 面读 [0..15] |
| 0x0240 | **WIRE_MAP** | 0x200 (128×f32) | **② 计算完成的数据**（中间量连线）：写者=扫描/seq |
| 0x0440 | LUT_DATA | 0x400 (256×f32) | LUT 原语数据 |
| 0x0840 | ROUTE_TABLE (active) | 0x800 (128×16B) | 路由表（引擎执行的"程序"）|
| 0x1040 | ROUTE_STAGING | 0x800 | 路由表 staging（部署写这里，RELOAD 后换）|
| 0x1840 | PARAM_TABLE | 0x1000 (128×16B) | 参数槽（4×f32/条）|
| 0x2840 | STATE_TABLE | 0x1000 (128×16B) | 状态槽（PID 积分/TIMER 累计等跨拍状态）|
| 0x3840 | RSVD_DSL_DOMAIN | 0x7C0 | DSL 预留（内含 TICK_STATS 0x3854）|
| 0x4000 | SEQ_TABLE | 0x400 (64×16B) | 顺序步条目 |
| 0x4400 | SEQ_CTRL | 0x80 (8×16B) | 顺序实例控制块 |
| 0x4480 | ROUTE_BUCKETS (active) | 0x1B8 | 分档桶表 |
| 0x4638 | ROUTE_BUCKETS_ST | 0x1B8 | 分档桶表 staging（与表成对切换）|
| 0x47F0 | FORCE_MASK / FORCE_VAL | 0x2B0 | 强制表（fmask/fval）|
| 0x4AA0 | MB_SET | 0x80 | Modbus 写区（上位机设定值 → SRC_HMI 源）|
| 0x4B20 | MB_CTRL/RX/TX/HOLD | 0x2C0 | Modbus 控制块/收发缓冲/HOLD 读区 |
| 0x4DE0 | CMD_REQ | 0x1000 | 免串口命令请求区 |
| 0x5DE0 | MACRO_CTRL/CODE | 0x1010 | macro 控制块 + 字节码 (4KB) |
| 0x6E00 | HIL_DUTY / HIL_FB_RAW | 0x8 | W5 观测面 |
| **0x6E08** | **空闲** | **0x11F8 (4.6KB)** | **← shadow 的家（§4）** |

---

## 3. 三个数据的当前位置（你问的三个"放哪里"）

### ① 搬进的数据（输入采样）
**`SENSOR_MAP` @ SHM+0x0040**（256B）。
- 写者：`di_poll`（每 100 拍写 [3..6]）/ `adc_poll`（状态机推进，[8..10]、[2]）
- 数据年龄：DI ≤1 拍 + 去抖；AI ~4 拍；HIL 反馈 25.6ms 均值
- **每拍更新** —— 它是全链最"新鲜"的数据。

### ② 计算完成的数据
**`WIRE_MAP` @ SHM+0x0240** + **`ACTUATOR_STATUS` @ SHM+0x0140**。
- 写者：扫描（engine_scan_itcm，受 gate&&RUN 门控）+ seq（步号镜像）。
- **"计算完成"= 扫描+seq 执行完毕的那一刻** —— 此后到拍尾之间的 WIRE/ACTUATOR
  就是"本拍最终结果"，输出段读的就是它。
- ⚠ STOP 态下扫描不跑 ⇒ WIRE/ACTUATOR 冻结（`eng_outputs_safe` 会把 ACTUATOR 清 0）。

### ③ shadow 写好了的（锁存缓冲）
**当前不存在** —— `do_poll` 是"打包后直接写 GPIOE_ODR"，没有中间槽。
**建议新增**：`OFF_DO_SHADOW = 0x6E08`（4B，SHM 尾部空闲区），内容 = `do_pack()` 的
16 位位图。理由见 §5。

---

## 4. 竞态分析（"锁存内容被覆盖"到底会不会发生）

引入 shadow 后的数据流（单缓冲）：

```
拍 N:   输入段 → 计算段 → do_pack → 写 shadow(值A)
t=拍N边界: TIM1 上溢 ⇒ MDMA 读 shadow(值A) → GPIOE_ODR  ┐ 与拍中断 N+1 入口
拍 N+1: 输入段 → 计算段 → do_pack → 写 shadow(值B)       ┘ 同时发生(总线仲裁)
t=拍N+1边界: MDMA 读 shadow(值B) → GPIOE_ODR
```

逐个场景：

| 场景 | 结果 | 是否有害 |
|---|---|---|
| 正常：CPU 拍内写 shadow → 边界 MDMA 读 | 锁到本拍值 | ✅ |
| CPU 写跨过边界（计算超长/ISR 延迟）| MDMA 读到**上一拍**的值 ⇒ **锁存延迟一拍**，数据仍一致 | ✅（预算门使概率极低）|
| CPU 连续两次写之间 MDMA 没来得及读（值被覆盖）| 中间值被跳过，两个变化合并成一次锁存 | **对电平输出无害**（ODR 是电平语义，只看最终值）；只有做**脉冲/边沿输出(PTO)** 时才需要双缓冲 |
| 撕裂（读到半新半旧）| shadow 是 4B 对齐单字写，AHB 单字写原子 | ✅ 不存在 |
| shadow 被 WIRE/ACTUATOR 更新波及 | 物理分区分离（0x6E08 vs 0x240/0x140）| ✅ 不存在 |

**结论：单缓冲 shadow 在"电平型 DO"语义下天然安全 —— 最坏情况是延迟一拍（数据一致），
不存在撕裂或意外覆盖。** 双缓冲只在引入边沿/脉冲类输出时才需要，现在不做。

---

## 5. 松散 vs 紧凑：需不需要优化

**太松散？** SHM 有历史空洞（CMD_REQ 4KB、RSVD 区、尾隙 4.6KB），但 **DTCM 总余量
63.5KB** —— 内存在本平台不是稀缺资源，空洞里还躺着有审计价值的保留域。**重排分区
收益低、风险高（全部 OFF_ 与 PC 侧同步），不做。**

**太紧凑？** 分区物理分离，WIRE/ACTUATOR/shadow 互不重叠，"覆盖"只发生在 shadow
自身的拍间更新上（见 §4，电平语义无害）。**不存在"锁存内容被计算覆盖"的通道。**

**需要做的优化只有一件（增量 4~8 字节）：**

```c
#define OFF_DO_SHADOW      0x6E08   /* u32: DO 打包位图（MDMA 锁存源）*/
#define OFF_DO_SHADOW_SEQ  0x6E0C   /* u32: shadow 写序号（诊断"锁的是第几拍"）*/
```

- 放 SHM 尾部空闲区：符合"一切数据在 SHM"公理；pyocd 可直接读（写侧/锁存值两侧对照）；
- 可选的 `SHADOW_SEQ`：CPU 每写一次 +1，MDMA 锁存时若把 seq 也搬走（8B 传输），
  掉电后/运行中都能回答"GPIO 上的电平是哪一拍的" —— 诊断"延迟一拍"现象的钥匙。
- **不需要**双缓冲、不需要把数据搬出 DTCM、不需要重排现有分区。

---

## 6. MDMA 路径速记（对应该布局的传输参数）

| 项 | 值 |
|---|---|
| 源 | `SHM + OFF_DO_SHADOW` = `0x20008220 + 0x6E08` = `0x2000F028`（DTCM，MDMA 经 AHBS 可达）|
| 目的 | `GPIOE_ODR = 0x58021014` |
| 宽度/长度 | 8 字节（shadow + seq）/ 单块 |
| 触发 | TIM1 上溢 → DMAMUX → DMA2 哑传输(1字) → TC → MDMA（硬件链）|
| 频率 | 每拍 1 次 = 10 kHz ⇒ MDMA 占用率可忽略（余量 >1000 倍）|
