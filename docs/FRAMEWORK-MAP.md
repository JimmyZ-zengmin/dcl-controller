# FRAMEWORK-MAP — 框架组成部分与微调指南 (2026-09-12)

> 配套: `docs/PLAN-io-into-engine.md`(I/O 拍内方案) / `docs/ARCH-H723.md`(架构详述)。
> 本文回答三个问题: **每个组件干什么、谁在什么时候调它、动它之前要注意什么**。

---

## 0. 一张图: 两个执行上下文

```
上电: clock_init(400MHz) → DWT/ITCM 拷贝+VTOR(0xFC00) → 活体自检 → 协议层
      → ADC/DI/HIL/DO init → 安全态登记 → 观测锚定 → 进主循环
主循环: proto_poll → 落盘/重填/镜像/mb_hold/macro → 栈哨兵 → wfi
TIM2 每 100µs → ISR (ITCM):
   拍头(UIF/心跳/tick/热重载) → 输入段(di_poll/adc_poll)
   → 计算(force→扫描→seq, 受 gate&&RUN) → 输出段(hil_out_poll/do_poll)
   → mb_tick → ISR 时长/拍周期统计
```

**门控总则**: 输入面与通信**不受** ENGINE_RUN 门控（停机也要能看现场、能通信）；
输出面**受**门控，且门控写在**各域内部**（STOP ⇒ `eng_outputs_safe` ⇒ 各域清自己的物理面）。

---

## 1. 组件清单

| 组件 | 行数 | 职责 | 入口 | 被谁调 |
|---|---|---|---|---|
| **main.c** | 2592 | 装配 + ISR + 主循环 + 协议命令 | `main()` / `TIM2_IRQHandler` | — |
| **engine** | 1130+1028 | 拍内核: 路由扫描/分档桶/seq/部署/安全态/SHM | `engine_tick` `engine_seq_tick` `engine_reload_active` `eng_outputs_safe` | ISR |
| **di** | 99 | 4 路 DI 采样+去抖 → SENSOR[3..6] | `di_poll`(拍内) / `di_tick`(对照) | ISR 输入段 |
| **adc** | 263 | ADC1 16bit 驱动 + AI 3 路(非阻塞状态机) + HIL 反馈 | `adc_poll`(拍内) / `ai_tick`(对照) `adc_read`(自检) | ISR 输入段 |
| **hil** | 174 | PWM 输出臂(TIM3) + 反馈累加 | `hil_out_poll`(拍内) / `hil_tick`(对照) `hil_outputs_safe` | ISR 输出段 / 安全态 |
| **do** | 90 | 16 路 DO 面: ACTUATOR → GPIOE | `do_poll`(拍内) / `do_outputs_safe` | ISR 输出段 / 安全态 |
| **macro** | 264 | ms 级慢动作 VM (loop_ms 自节流) | `macro_tick` | 主循环 |
| **modbus** | 450 | 通信域 RTU 从站 (ISR 推进) | `mb_tick` `mb_refresh_hold` `mb_inject` | ISR / 主循环 |
| **transport** | — | 帧解析 + CRC16 | `fp_feed` `fp_init` | 主循环 proto_poll |
| **uart** | 164 | USART1 驱动 (RX 环形缓冲) | `uart1_write` `uart1_rx_pop` | 主循环 / USART1 ISR |
| **persist** | 358 | 落盘 (FLASH 最后一扇区) | `persist_save` | 主循环 |
| **flash** | 192 | FLASH 底层擦写 | `flash_erase_sector` `flash_write` | persist |
| **clock** | 206 | 400MHz PLL / 分频树 | `clock_init` | main |
| **regs** | 444 | 寄存器宏 (含 NVIC 安全宏) | — | 全部 |

---

## 2. SHM 布局速查 (DTCM `0x20008220` 起, 32KB)

| 偏移 | 内容 | 写者 | 微调注意 |
|---|---|---|---|
| 0x00 | MAGIC/VERSION/N_ROUTES/SCAN_MODE… | 协议 | 改布局必须同步 PC 侧 |
| 0x0D | ENGINE_RUN | 0x11/0x12 | 停机语义的开关 |
| 0x18-0x33 | TIMING (samples/pmin/pmax/emin/emax/…) | ISR | `pmin` 会被调试暂停污染, 判据用 `pmax` |
| 0x34 | GPIO_MASK (DO 管辖位) | 协议 | bit=1 ⇒ PEi 归引擎管; 高 16 位非 0 ⇒ `g_safe_mask_oob` |
| 0x40 | SENSOR_MAP[64] (f32) | di_poll/adc_poll | DI=3..6, HIL 反馈=2, AI=8..10 |
| 0x140 | ACTUATOR_STATUS[64] (f32) | 扫描/seq | DO 面读它; STOP 时被 `eng_outputs_safe` 清 0 |
| 0x240 | WIRE_MAP | 扫描/seq | HIL 读 WIRE[20] |
| 0x4000 | SEQ 区 | seq | 步号在 +4 |

---

## 3. 微调热点与地雷区 (改前必读)

### 3.1 拍内时序 (动 ISR 里任何东西之前)
- **ISR 段顺序有因果**: 输入段必须在扫描前(扫描读 SENSOR)、输出段必须在扫描后
  (读本拍 WIRE/ACTUATOR)、`mb_tick` 在门外(停机可通信)。挪动前想清楚依赖。
- **ITCM 余量**: 代码 `_eitcm=0x1808`, 向量表钉在 `0xFC00`, 中间 58KB 可长。
  **加新 ISR 调用函数**: 放 flash + `__attribute__((long_call))`(跨区必须 BLX);
  **不要用函数指针数组**(被 -O2 常量传播回直接 BL)。
- **拍预算**: `isr_max` 现约 **1509~1655 cyc**, 上限 `EXEC_BUDGET_CYCLES=32000`。
  新增功能后看 ④ 是否仍为 0。
- **采样-输出时序契约 (P3-C)**: 输出沿=拍头(MDMA 锁存) ⇒ 采样启动=拍尾(adc_poll_kick)
  ⇒ 两者间隔 ~97µs 建立时间。"自导自演"(输出沿紧贴采样孔径)已消除。
  **回收(拍头 reclaim)与启动(拍尾 kick)必须分处两处**, 不要合并回 adc_poll 整体。

### 3.2 参数旋钮 (按组件)
| 旋钮 | 位置 | 现值 | 说明 |
|---|---|---|---|
| DI 采样分频 | `di.h DI_SAMPLE_DIV` | 100 | 相位锚定, 表=严格 10ms |
| DI 去抖 | `di.h DI_DEBOUNCE` | 3 次 | =30ms |
| DI 引脚 | `di.h DI_PIN_*` | PC0-3 | 改脚+改 di_init 的时钟使能 |
| ADC 通道表 | `adc.c AI_CHS` | 16/17/18 | + `AI_PINS` 配 analog |
| ADC 时钟 | `adc.c PRESC` | **/2 ⇒ 12.5MHz** (BOOST=1 档极限) | fADC 上限 12.5MHz (H723 BOOST bit9 写不进) |
| ADC 采样时间 | `adc.c SMPR` | **32.5 周期 = 2.6µs** | 40kΩ 源 16 位需求 2.5µs 刚好满足; 勿再缩 |
| ADC 状态机 | `adc.h ADC_SM_*` | 起跑 3 拍/超时 8 拍 | 更新率=**2 拍/通道** (转换 4µs < 拍长, 流水线) |
| DO 影子/锁存 | `do.c DCL_DO_LATCH` | **0 (直写)** | =1 走 MDMA 锁存链 (P3-B: DMA2 哑桥已通, TEIF 待查) |
| HIL PWM | `hil.h HIL_PWM_HZ/RES` | 1kHz/1000 | `s_arr1` 由 init 缓存 |
| HIL 输入槽 | `hil.h HIL_U_WIRE` | 20 | WIRE[20] |
| DO 阈值/端口 | `do.c` | 0.5 / GPIOE | macro **不许**碰 PE |
| 拍预算 | `engine.h EXEC_BUDGET_CYCLES` | 32000 | 改了必须重测成本表(轴4) |
| 时钟 | `clock.h CLK_*` | 400MHz | H723 实测天花板 ≈465MHz |

### 3.3 新增"每拍动作"的规矩
1. 新变量 ⇒ **必须进 `obs_anchor()`**, 否则被 `--gc-sections` 回收 (nm 里消失)。
2. 新外设域 ⇒ `do_init` 式**就绪门**必须自带 (ISR 从阶段 8 就在跑)。
3. 新输出面 ⇒ `eng_register_output_surface(本域_safe)` 登记安全态回调。
4. 新协议命令 ⇒ CMD_ 宏 + dispatch + cap 位三处对齐 (审计轴 2 会查)。
5. 改 SHM 布局 ⇒ 全部 OFF_* + PC 侧 + `_Static_assert` 同步。

---

## 4. 验证工具链 (改完必跑)

| 工具 | 作用 | 用法 |
|---|---|---|
| `build.sh` | 构建(零警告闸门+tmp 重试) | `bash build.sh [-DDCL_XXX=n]` |
| `tools/h723_io_isr_check.py` | 拍内 I/O A/B 验收 (判据①-⑥) | `--tag DUT/CTRL`; 判据② 看 **pmax** |
| `tools/h723_pe_probe.py` | GPIO 端口可用位实测 | 直接跑 (含用完复位+核对) |
| `tools/h723_audit_full.py` | 协议/判据/成本表审计 | `--offline` 8 项; 在线项需串口 |
| `tools/h723_op_sweep.py` | 成本表重测 (改内核后) | `--dur 0.3 --json build/op_cost.json` |
| pyocd | 烧录/读写 | `pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex` |

**改 linker 后必须 clean 重build** —— 只改 .ld 不触发重链接 (`identical` 是信号);
改完核对 `grep -E "_vtor_itcm" build/dcl_h723.map`。

---

## 5. 已知地雷 (历史事故, 不许再踩)

1. **VTOR 对齐**: 必须 ≥ 4×166 取 2 的幂 = **1024**; 表已钉 `0xFC00`。
   128 对齐时硬件掩码会把 TIM2(44) 掩到 12(保留→Default_Handler)。
2. **常量传播**: `static const` 函数指针数组会被 -O2 折成直接 BL —— 跨区调用用
   声明上的 `long_call`。
3. **ECC**: ITCM 64 位宽+8ECC 位, 不可关。**未初始化区域被读会出错** ⇒
   新增 NOLOAD 区必须像向量表一样**先全宽初始化再用**。
4. **一个输出面一个驱动者**: do_poll 与 macro 别共引脚; hil_tick 交付档已整段让位。
5. **空判据**: 恒 0 的观测量先怀疑"从没被走到"; 判据必须能失败。
6. **恒 0 的 pmin 是 halt 污染**: 判拍周期用 `pmax`。
