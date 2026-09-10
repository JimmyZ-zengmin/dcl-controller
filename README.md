# 9.10 H723newest — DCL 引擎 · STM32H723 平台（干净重建线）

从 `esp32-core0`（ESP32-S3）迁移到 **STM32H723ZGT6** 的新起点。
本目录是**独立项目**：与旧的 `../h723-core0`（历史探索项目）**无代码继承关系**——
只借鉴其"硬件事实"级别的发现，其余结论一律在本线重新实证。

---

## 硬件与仪器

| 项 | 说明 |
|---|---|
| 板 | 鹿小班 LXB723ZG-P1（STM32H723ZGT6，1MB Flash，Rev Z / REV_ID 0x1001）|
| 晶振 | **HSE 25MHz**（板上无源晶振，已 LA 实测反推确认 25.000MHz）|
| 调试 | DAPLink（CMSIS-DAP）→ `pyocd`，目标名 **`stm32h723xx`** |
| 串口 | DAPLink VCP（COM11）+ CH340（COM14）|
| 逻辑分析仪 | Saleae Logic2（Logic 8，无模拟输入），MCP @ `http://127.0.0.1:10530/` |
| 接线 | `LA CH4 ← PA8`（拍输出），GND 共地 |

---

## 当前状态（2026-09-10）

- ✅ **工具链**：GCC 7.3.1（STM32CubeIDE 自带）+ cmake 4.0.3 + ninja 1.12.1 + pyocd 0.44.1
- ✅ **时钟**（HSE 25MHz → VOS0 → PLL1，M=5 / N=80 / P=1 / 整数模式）：
  ```
  CPU 400MHz · HCLK 200 · APB 100 · TIMxCLK 200   (全链路整数)
  FLASH_ACR: LATENCY=3, WRHIGHFREQ=3
  ```
- ✅ **100μs 拍**：TIM2，`ARR = 20000-1`；
  **LA 外部实测：边沿间隔众数 100.0000 μs（96% 命中）→ 反推 CPU 400.00 MHz**
- ✅ **抖动**：剔除线上毛刺后，边沿间隔极差 = **1 个采样点**（16MHz → 62.5ns）
  → 真实抖动低于 LA 分辨率
- ✅ **阶段 1 空拍骨架**（详见 `docs/STAGE1-REPORT.md`）：
  空拍 ISR **95 cyc** 典型（min 79 / max 95 → 极差 40ns）@400MHz；
  **拍周期抖动 = 0 个 CPU 周期**（min = max = 40000 cyc，即 <2.5ns，比 LA 的 62.5ns 更细）；
  DWT 经外部仪器标定：40000 cyc ÷ LA 实测 100.0000μs = **400.00 MHz**
  · 对照 S3：空拍 49 cyc @240MHz = 204ns；本平台 95 cyc @400MHz = 237ns
- ★ **阶段 1 关键发现**：flash 常驻 ISR 的成本被**代码布局 / 取指**主导 ——
  同一份 ISR 因构建不同给出 **86 / 95 / 98 cyc**，而且**删掉 10 条指令反而更慢**。
  ⇒ 阶段 2 把 ISR 搬 ITCM 是**必要**（不是优化）：否则 WCET 在 ±10% 量级上不可预测。
- ℹ️ **线上毛刺**：PA8 约 0.01~0.12% 的边沿间隔异常短（几十~几百 ns），
  疑为跳线串扰或 LA 采样；**不影响拍周期**（众数稳定 100.0000μs）。
  测频工具已按"异常项单独计数"处理，不用 min/max 下结论。
- ⚠️ **精度口径**：LA 自身时基精度约 0.1% —— 上述"频率一致"应读作
  **0.1% 量级一致**，不应用末位小数声称更高精度。
- ⚠️ **本板实测频率天花板 ≈465MHz**（450/460 可跑，470 起不运行）
  → 数据手册标称 550MHz，差距原因指向 **VCORE 实际电压或板级供电/VCAP**
  → 详见 `docs/REF-frequency-ceiling.md`（含完整排除清单）

---

## 目录结构

```
src/        平台与引擎代码
  clock.c/h   时钟树（改 CLK_PLL1_DIVN1 即换主频）
  regs.h      H723 寄存器定义（自写，不依赖 CMSIS）
  main.c      时钟引导 + 100μs 拍 + PA8/PA9 输出 + DWT 拍开销测量 (ISR_MODE 可切形态)
  syscalls/sysmem.c  newlib 桩
ld/         链接脚本（含 DTCM + ITCM 段 —— 迁移的核心价值）
startup/    ST 启动文件（BSD-3 厂商模板）
cmake/      工具链文件
tools/      h723_ports.py（串口自检）· h723_stage1_read.py（读回 DWT 测量）
  legacy/   clock_probe.sh / ws_scan.sh（早期 SWD 探测，已被证伪，留档）
docs/       迁移方案 + 时钟依据 + 频率天花板 + 阶段1报告 + 硬件接线
```

---

## 常用命令

```bash
bash build.sh                                    # 构建 (ELF 无扩展名 + .bin/.hex/.map)
bash build.sh clean                              # 清理

pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex

python la_tick_freq.py --cpu 400 --ch 4                     # LA 外部测拍频率
python la_tick_freq.py --cpu 400 --ch 4 --rate 16000000      # 看抖动

python tools/h723_stage1_read.py --run 3         # 读回 DWT 测量 (空拍成本/抖动/标定)
python la_tick_freq.py --cpu 156 --ch 1 --rate 1000000 --dur 2.0   # 验证 CH1←PA9 线路

bash sweep_freq.sh 80 90 92 94                   # 频率天花板扫描 (N → 400/450/460/470MHz)
```

**失败指示**：时钟初始化失败时固件会在 PA8 上闪 `|错误码|` 次（错误码见 `src/clock.h` 的 `CLK_ERR_*`），
然后用 LA 一抓就知道卡在哪一步 —— 不依赖 SWD。

---

## 开发纪律（沿用 esp32-core0 收口教训）

1. **宣称必须等于实现** —— 每个功能都要有可复跑验证
2. **频率/时序类结论必须用 LA 外部证据** —— SWD 在 CPU 跑不住时会失步，
   会给出**完全错误**的结论（本线已亲历：SWD 报"300~340MHz 断崖"，LA 实测 450MHz 正常）
3. 用 SWD 读"运行态"寄存器前**不要先 reset** —— `pyocd reset halt` 会把 RCC 打回复位默认值
4. 改协议版本号必须**全局搜索**硬编码断言
5. 新增"域"必须登记到冷启动复位清单；新增内存区必须补 SHM 布局断言
6. 读**运行态**统计必须**单会话**完成（连接 → reset → sleep → 读）：
   默认 halt 连接在目标调试态异常时会报 `No cores were discovered`，
   用 `connect_mode=under-reset` 恢复
7. `--gc-sections` 会回收**无人读**的全局（`volatile` 也保不住）→ 观测变量必须真被读
   否则它会从符号表消失（`g_isr_mode` 就这么没了）

---

## 与 esp32-core0 的关系

- `esp32-core0`（ESP32-S3）已于 2026-09-10 **冻结：只修不增**；功能增益全部转到本线
- 引擎架构（100μs 硬拍 / 桶化分档 / 原语表 / 四域：连续·逻辑·顺序·通信）是**平台无关**的；
  迁移的是**平台层**（定时器、内存、UART、ADC、GPIO、persist、周期计数）
- 迁移方案与不变量清单：`docs/MIGRATE-H723.md`

---

## 下一步（阶段 2 — 内存分区 + 表扫描）

1. 链接脚本分 ITCM / DTCM：**ISR 搬 ITCM** → 复测空拍成本
   （检验阶段 1 的假设：预期抖动收窄到 0~2 cyc、成本降到接近真实指令数）
2. 引擎表搬 **DTCM**，移植路由扫描（先全表扫，不做分档）
3. DWT 测「单条路由」周期数（对照 S3 的 234 cyc）
