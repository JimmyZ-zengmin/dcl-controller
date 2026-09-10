# H723 迁移开发规划（执行版 — 连续开发直到达到 S3 完成度）

> 这份文档是**给我自己照着做的执行指令**，不是给人读的说明书。
> 基线: `31c2681` · 目标: 命令覆盖 3/20 → 18/20（2 条不适用）
>
> **核心纪律**: 每一步写完立刻测，测试绿了才进下一步。**不停下来等审计。**

---

## 执行总则

1. **一次只做一步**，做完跑该步的验收命令，§"验收"全绿才动下一步
2. 每步完成后 `git commit`（消息写清"做了什么 + 实测数字"）
3. 每步完成后追加记录到 `.workbuddy/memory/2026-09-11.md`
4. 遇到失败**不停**，自己排查修好继续（这是"先开发后审计"的代价）
5. 任何新 SHM 域 → 三件事同时做：偏移宏 + `_Static_assert` + `cold_start_reset()`
6. 任何新命令 → `DCL_CAP_H723_IMPL` 与 `DCL_CAP_H723_NOTYET` 同步改

---

## W1: 运行控制 + SHM 读写（P0，7 条命令）

### W1.0 前置：地址白名单适配
```
S3 白名单（main.c:83-104）:
  g_shm_base .. +SHARED_MEM_SIZE  →  H723: g_shm .. +SHM_SIZE (DTCM 0x20000000)
  0x60004000-0x60005000 (S3 GPIO) →  H723: 0x58020000-0x58022000 (GPIOA-K, 各 0x400)
  0x60009000-0x6000A000 (S3 LEDC) →  删掉（H723 无 LEDC）
  0x60000000-0x60100000 (S3 外设) →  H723: 0x40000000-0x40025000 (APB1/2)
                                        0x58000000-0x58025000 (APB3/4 + AHB)
```
★ **H723 的 SERIAL/USB 区不要开放写** —— 误写 RCC/PWR 会砖。

**验收**: 单元级 —— 用 0x20 读 `g_shm` 首字，读到 `SHM_MAGIC`；读 `0x58024400`(RCC) 返回 NAK。

### W1.1 `0x20 READ` / `0x22 READ_BURST`
- 载荷 `[addr:u32]` / `[addr:u32][count:u16]`
- 守卫：`valid_addr` / `valid_range`；count 上限 256（S3 值）
- H723 `FRAME_PAYLOAD_MAX = 6150`，1024B 应答放得下（S3 T19 验过 1024B）

**验收**: `python tools/h723_shm_rw.py --read-smoke`
- 读 SHM 首字 == MAGIC
- 读 RCC 基址 → NAK
- burst 读 1024B 有完整应答（T19 等价）
- 越界 burst → NAK

### W1.2 `0x21 WRITE` / `0x23 WRITE_BURST`
- 守卫：`valid_addr` + `write_allowed`（NaN/Inf 防护）
- **必须逐字搬** `shm_off_is_float()` 的区域表（H723 的 OFF_* 名不同但语义同）
- burst：**先全量预检再写**（避免半写后才发现非有限值）

**验收**: `python tools/h723_shm_rw.py --write-smoke`
- 写 WIRE_MAP[0] = 3.14 → 回读 3.14 ✅
- 写 0x7F800000 (Inf) 到 WIRE_MAP → NAK `non-finite rejected`
- 写 0x7FC00000 (NaN) 到 PARAM_TABLE → NAK
- burst 写 256 字 → ACK（T24 等价）
- burst 中间夹一个 NaN → **整体 NAK 且前段未被写**（P1b 语义）

### W1.3 `0x11 START` / `0x12 STOP` / `0x13 RESET`
逐字搬 S3 语义，三处必须保留：

**START**（S3 main.c:895-921）
```c
/* ① F11 预算兜底：persist 恢复的毒药表拒 START */
if (nr && engine_prog_budget(route_table, nr) > EXEC_DEPLOY_BUDGET) → NAK "prog exceeds budget"
/* ② OA13 幂等：只在 STOP→RUN 转变时 reset 统计（否则 T4"心跳连续"判据被误伤）*/
if (!ENGINE_RUN) timing_stats_reset();
ENGINE_RUN = 1;
/* ③ Seq 运行态重置（W3 落地后再补；当前 N_SEQ=0 时是空循环）*/
```

**STOP**（S3 main.c:925）
```c
ENGINE_RUN = 0;
outputs_safe();   /* P1-2 安全态：执行器归零 */
```
★ H723 的 `outputs_safe()`：S3 是 `GPIO_OUT_W1TC_REG` 只清不置。
H723 用 **BSRR 的高 16 位**（`GPIOx_BSRR = mask << 16`）实现"只清不置"，
掩码取 `OFF_CTRL_GPIO_MASK`；执行器数组 `memset` 为零。

**RESET**（S3 main.c:927）→ `cold_start_reset()`

**验收**: `python tools/h723_runctrl.py`
- STOP 后 `ENGINE_STATUS.run == 0`，且 `outputs_safe` 生效（GPIO ODR 对应位 = 0）
- RESET 后 timing 归零、表清空、ENGINE_RUN = 0
- START → run=1，samples 开始增长
- 二次 START → samples **不清零**（幂等，OA13）
- 毒药表（手工 deploy 一个超预算程序后 RESET 表、伪造 N_ROUTES）→ START 被 NAK

### W1.4 协议回归闸门
**验收**: `python tools/h723_proto.py --port COM7` 仍 **12 PASS / 0 FAIL / 0 SKIP**

### W1 收口
- `DCL_CAP_H723_IMPL` 增加位（HMI 0x0200 是 W1 之后可报的？**否** —— HMI 是 SRC_HMI 源，
  属阶段 4。W1 只报已有的 MULTICYCLE|HOTRELOAD|WIRE2|VERINFO，**不加位**）
- 更新 README 命令覆盖表
- commit + memory

---

## W2: Force + persist（P0）

### W2.1 SHM 开 Force 区
```
当前 OFF_RSVD_EXEC_DOMAIN = 0x47F0, SZ = 0x2B0 (704B)
改为:
  OFF_FORCE_MASK = 0x47F0   /* 4 × u32 = 16B, MAX_WIRES/32 */
  OFF_FORCE_VAL  = 0x4800   /* 128 × f32 = 512B */
  OFF_FORCE_END  = 0x4A00
  → 剩 0xA0 (160B) 作 OFF_RSVD_EXEC_TAIL
断言: 0x47F0 + 16 + 512 + 0xA0 == 0x4AA0 (OFF_MB_SET)  ← 必须精确相接
```
★ **三件事同时做**：偏移宏 + `_Static_assert` + `cold_start_reset()`（本实现整段 memset，天然覆盖）。

### W2.2 ISR 侧 Force 语义
S3 在 ISR 拍首执行（`core0_isr.c`）：
```c
/* 拍首：被强制的 wire 用 FORCE_VAL 覆写 */
uint32_t *fmask = SHM_PTR(OFF_FORCE_MASK);
float *fval = SHM_PTR(OFF_FORCE_VAL);
float *wm = SHM_PTR(OFF_WIRE_MAP);
for (int w = 0; w < MAX_WIRES; w++)
    if (fmask[w>>5] & (1u<<(w&31))) wm[w] = fval[w];
/* 扫完后：路由写端屏蔽 —— 有 FORCE 位的 wire 不写 */
```
★ **OA9 教训**：强制值**必须写 FORCE_VAL**。只写 WIRE_MAP 的话下一拍即被抹成 0。
S3 旧版 13/13 全绿是因为测试全用 `val=0.0`，bug 与期望重合（**判据盲区**）。
⇒ H723 的测试**必须至少有一个非零 val 的用例**。

### W2.3 `0x24 FORCE`
```c
载荷 [idx:u16][mode:u8][val:f32]   mode 0=释放 1=强制
守卫: idx < MAX_WIRES / mode <= 1 / mode==1 时 is_finite_bits(val)
强制: MASK 置位 + FORCE_VAL[idx]=val + WIRE_MAP[idx]=val
释放: MASK 清位 + FORCE_VAL[idx]=0（清残留）
```
**deploy/RESET 必须 clear force**（S3 的 `force_clear()`）。

**验收**: `python tools/h723_force.py`
- 强制 wire[3] = **7.5**（非零！）→ 引擎跑 ≥3 拍后回读 wire[3] == 7.5（证明拍首覆写生效）
- 部署一条"写 wire[3]"的路由后跑 → wire[3] 仍 == 7.5（证明写端屏蔽生效）
- 释放 → 下一拍 wire[3] 被路由覆写回路由值
- 强制 Inf → NAK
- 强制 idx=999 → NAK
- deploy 后 force 被清（回读 MASK == 0）

### W2.4 persist（裸 Flash 双扇区）
S3 用 NVS，H723 改用 **Flash 扇区 A/B 双副本**：
```
扇区 15 (0x080E0000, 128KB) = 副本 A
扇区 14 (0x080C0000, 128KB) = 副本 B
每份: [magic:u32][seq:u32][len:u32][payload][crc32]
写入: 擦目标扇区 → 写 → 更新 seq → 校验回读
加载: 比较 A/B 的 seq 取大者，校验 CRC 失败则回退另一份
```
内容 = 整套 route/param/state 表 + N_ROUTES/N_PARAMS/N_STATES + PROG_MAGIC。

★ H723 Flash 擦写要点：
- FLASH_CR 的 `PG`/`SER`/`START`；写前必须 `FLASH_SR.BSY == 0`
- 擦除时间 **~1-4 秒/扇区**（128KB），**绝不能在 ISR 或拍内做**
- 写前 `FLASH_ACR` 不用改（我们跑在 VOS0 400MHz，ST 要求写 Flash 时 HCLK 有限制 —— 需查 RM0468 §3.3.5，可能要临时降频或用 `FLASH_CR.BKER`）
- **A/B 双副本的意义**：擦除中掉电不丢旧数据 → 这才是"掉电保持"的真判据

**验收**: `python tools/h723_persist.py`
- deploy 一个程序 → 0x43 save → 断电（pyocd reset）→ 上电自动加载 → 表校验和一致
- 双副本 seq 递增验证
- 擦除中复位（pyocd reset 打断）→ 上电仍能加载旧副本（**这才是真掉电判据**）
- persist 恢复超预算程序 → START 被 NAK（与 W1.3 的 F11 兜底联动）

**W2 收口**: 能力位加 `PERSISTENT (0x0004)` + `FORCE (0x0080)`；
`DCL_CAP_H723_NOTYET` 同步减两项。`_Static_assert((IMPL & NOTYET) == 0u)` 必须是绿的。

---

## W3: Sequencer（P1，1 条命令）

### W3.1 SHM 开 Seq 区
```
当前 OFF_RSVD_DSL_DOMAIN = 0x3840, SZ = 0xC40 (3136B)
改为:
  OFF_SEQ_CTRL = 0x3840   /* MAX_SEQ_INST × sizeof(SeqCtrl_t) */
  OFF_MACRO_RUN = ...     /* W5 用 */
  OFF_DSL_TAIL = ...
断言: 0x3840 + seq_sz + macro_sz + tail_sz == 0x4480 (OFF_ROUTE_BUCKETS)
```
先读 `DESIGN-sequencer.md` + S3 `main.c:650` `h_seq_deploy` 确认 `SeqCtrl_t` 字段。

### W3.2 `0x44 SEQ_DEPLOY` + ISR 侧推进
S3 语义要点（DESIGN-sequencer.md）：
- 顺序域与连续域**同拍共存**：Seq 在 ISR 内推进，但**不走分档桶**
- `step_tick` 计数到 `step_dur` 换步；`run` 位控制启停
- 步号镜像到 `out_wire`（START 时置 1.0）
- START = 运行态重置（全部实例回第 1 步），组态保留

**验收**: `python tools/h723_seq.py`（对照 S3 `verify_seq.py` + `verify_seq8.py`）
- 部署 3 步序列，步长 10/20/30 拍 → 用 0x20 高频轮询步号，验证换步时刻
- 8 实例并发（S3 verify_seq8 口径）
- STOP→START 后全部回第 1 步
- 步号镜像 wire 值正确

**W3 收口**: 能力位加 `SEQ (0x0040)`。

---

## W4: Modbus 通信域（P1，3 条命令 + 370 行）

### W4.0 前置决策：USART2 还是单口复用
- **方案 A（推荐）**: 配 USART2（PD5=TX/PD6=RX）作 Modbus 物理口
  —— 与 S3 的双口拓扑一致，且 USART1 保持协议口不被干扰
- **方案 B**: 单口复用（协议帧与 Modbus 帧靠 SYNC 区分）—— 有耦合风险

先查 H723 板子的 PD5/PD6 是否引出（读板商原理图 + `docs/HARDWARE-PINOUT.md`）。

### W4.1 移植 `modbus.c`
S3 的 370 行，关键点：
- `mb_tick()` **在 run 门外、计时前**（每拍跑，不依赖另一核）—— 单核上反而更简单
- 每拍限速：不能每拍都发完整帧
- CRC16-Modbus（poly 0xA001，与协议 CRC16-CCITT 不同！别混）
- 从站地址 / 寄存器映射表来自 `OFF_MB_SET`

### W4.2 三条命令
```
0x60 MB_INJECT [frame]  —— 注入 Modbus 帧（LA 验证用）
0x61 MB_RESP            —— 读响应 + 状态
0x62 MB_CFG [baud][addr][mode] —— 配置
```

**验收**: `python tools/h723_modbus.py` + **LA 外部验证**
- 注入一条 Modbus RTU 读请求 → 收到合法响应帧（CRC 校验过）
- **LA 抓 USART2 TX 脚，硬件解码 Modbus RTU** → 逐字节比对（S3 是 7/7 PASS）
- ★ 这条**必须有 LA 外部证据**，不能只靠 PC 侧自收

**W4 收口**: 能力位加 `COMM (0x0100)`。

---

## W5: 外设域（P2）

### W5.1 ADC 16 位 + DMA → SENSOR_MAP
- H723 ADC1/ADC2/ADC3，16 位，多通道扫描 + DMA 循环
- 映射到 `OFF_SENSOR_MAP[0..n]`
- ★ 替代 S3 的 DHT22（T3 套件改用 ADC 通道做等价验证）

**验收**: 输入已知电压（或接 3.3V/GND 做两点标定）→ 读回值线性度检查

### W5.2 macro（字节码 VM，331 行）
- `macro_loop.h` 234 + `macro_exec.h` 97
- SHM 需要 `OFF_MACRO_RUN` 区（W3.1 已预留位置）
- 命令 `0x30 MACRO_UPLOAD` / `0x31 MACRO_CTRL`

**验收**: 对照 S3 `T14 MACRO wire写读回` + `T21a VM越界idx掩码到72`

### W5.3 DI / HIL / AI（499 行）
- `di.h` 92 —— 数字输入（H723 GPIO IDR 替代 S3 gpio_ll）
- `hil.c` 178 —— 硬件在环
- `ai.c` 143 —— 用 16 位 ADC 精度提升

### W5.4 ST 标准语言前端（S3 也没有，属超越项）
- 不在"达到 S3 完成度"范围内，**做完 W5 再评估**

---

## 最终验收（达到 S3 完成度）

### 命令覆盖
```
目标: 18 / 20 实现
不适用 2 条: 
  0x50 SHELL    (依赖 fs/shell，不迁)
  display 0x70-0x72 (H723 无 TFT)
```

### 测试套件
```
python tools/h723_test_dcl.py  ← 从 S3 test_dcl.py 移植
目标: 18 套全绿 + 2 套 SKIP（T5 Shell / T11 FS）
T3 改用 ADC 通道（不是 DHT22）
```

### 确定性指标（必须不劣于 S3）
| 指标 | S3 | H723 目标 | 实测 |
|---|---|---|---|
| 拍周期 | 99.9973 μs | ≥ 100.0000 μs | 待测 |
| 拍抖动 | 极差 0 cyc | 0 cyc | 待测（含协议层后可能 ≤4 cyc，须诚实标注）|
| 单路由 | 234 cyc | < 100 cyc | 56.0 ✅ 已达标 |
| 分档收益 | 2.59× | ≥ 2.59× | 2.59 ✅ 已达标 |
| 热重载 | — | < 1 拍 | 3.6μs ✅ 已达标 |

### 一次性审计（全部 W 完成后）
对照 S3 的 4 轮审计口径做一遍：判据可失败性 / 宣称=实现 / 对端视角 / 成本表受控对照。

---

## 附：本次不再重复做的事

- ❌ **不写新设计文档** —— 有 MIGRATE-H723.md / DESIGN-*.md 够了
- ❌ **不重构已绿的代码** —— 引擎/分档/热重载已优于 S3，只补命令面
- ❌ **不迁 display / fs / shell / dht22** —— 见 STATUS-2026-09-11.md §4
- ❌ **不每步等审计** —— 改为"每步必测 + 波次回归 + 最后一次性审计"
