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
- ★ **阶段 2 完成**（详见 `docs/STAGE2-REPORT.md`）：ITCM/DTCM 落位 + 路由扫描移植
  + **同镜像 A/B 实验**。一句话结论：
  > 同一份机器码（2136 B 逐字节相同，已双向验证），
  > FLASH 里跑 **203.6 cyc/路由**，ITCM 里跑 **56.0 cyc/路由**。
  > 128 条全表扫：FLASH 26229 cyc（占拍 **65.8%**）vs ITCM 7225 cyc（**18.3%**）。
  > 换成 19 原语混合程序，FLASH 版冲到 **98.9% 拍占用**（几乎锁死），ITCM 版只用 29.0%。

  | 组 | 配置 | eng_min | 占拍 |
  |---|---|---|---|
  | A | 骨架（不扫描）| — | isr **44 cyc** |
  | B1 | FLASH 全表128·全DIRECT·IC关 | 26229 | 65.84% |
  | B2 | **ITCM 全表128·全DIRECT·IC关** | **7225** | **18.34%** |
  | C1 | FLASH 全表128·19原语·IC关 | 38923 | **98.86%** |
  | C2 | ITCM 全表128·19原语·IC关 | 11419 | 28.98% |
  | E | ITCM 全表128·全PID·IC关 | 15275 | 44.78% |
  | F1 | FLASH 全表128·全DIRECT·**IC开** | 8164 | 20.70% |
  | F2 | ITCM 全表128·全DIRECT·**IC开** | **7225** | 18.34% |

  - **I-cache 对照（决定性）**：开 I-cache 后 FLASH 26229→8164（3.2×），
    而 ITCM **7225→7225（逐位相同）** ⇒ ITCM 的成本与 cache 状态无关，
    FLASH 的成本取决于一个引擎不控制的状态变量。即使开着 cache，
    FLASH 仍要 62.69 cyc/条 > ITCM 的 56.02。
  - **同一 flash 函数换个地址（+0x250 B）成本变 5%**（26229 ↔ 24986）
    ⇒ flash 常驻热代码的 WCET 无法预算，这正面证实了阶段 1 的假设。
  - **拍周期与引擎负载解耦 —— 前提是 ISR 在拍内跑完**（口径见审计报告 H7）：
    组内极差 0（周期恒 40000）只在"没超载"时成立；本轮审计期间真的踩到过一次超载
    （FLASH·混合组一次构建落到 **41166 cyc = 102.9% 拍**，拍周期被拉到
    41334~41910、极差 **576 cyc = 1.4 µs**）。工具已加超载判定。
  - ★ **FLASH 版成本随构建布局摆动 ±3%，而拍长 40000 恰好落在摆幅中间**：
    同一个引擎在三次构建里给出 B1 `26229 / 27818 / 26229`、
    C1 `38923 / 41166 / 38813`，**ITCM 版三次都是 7225 一字不差**。
    ⇒ "这个程序跑不跑得下"由构建布局决定，不由程序决定。
  - ★ **落位机制已实测坐实**（`docs/REF-flash-placement.md`，16 次构建）：
    用 `-DSCAN_FLASH_PAD=N` 只平移 `engine_scan_flash` 的地址（一条指令不改），
    FLASH·全DIRECT 在 **24957~28827（极差 15.5%）**、混合程序在 **36967~41500（12.3%）**
    之间变化，**而 ITCM 控制组 16/16 恒为 7225（极差 0）**；
    成本是 **`addr mod 32` 的确定函数**（8 个同余类各双样本，逐位相同）。
    ⇒ ① "用 flash 地址算 WCET 预算"不可能；② "编译过+实测跑得动"不是证据；
    ③ 热代码进 ITCM 是**设计前提**而非优化。
  - ★ **审计修正**（`docs/AUDIT-H723-stage2.md`，对照 esp32-core0 最后两轮审计口径）：
    哨兵从"恒真的 `route[0].op`"改为**整表 FNV-1a 校验和 + Python 独立预测逐组比对**；
    补 SHM `0x00-0x3F` 名字与断言、栈哨兵、`SRC_HMI` 显式分支、`cold_start_reset` 单一入口。
  - 空拍外壳：ITCM **44 cyc** vs FLASH **160 cyc**（3.6×）。
  - 单条路由 **56.0 cyc = 140 ns**（对照 S3 的 234 cyc = 975 ns，快 7 倍）。
- ★ **阶段 1 关键发现**（已被阶段 2 证实）：flash 常驻 ISR 的成本被**代码布局 / 取指**主导 ——
  同一份 ISR 因构建不同给出 **86 / 95 / 98 cyc**，而且**删掉 10 条指令反而更慢**。
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
  engine.h    表结构 / SHM 偏移 / OP·SRC 码（与 esp32-core0 逐字节同构 + 布局断言）
  primitives.h 19 原语移植（算法逐字保留）
  engine.c    SHM(DTCM) + 冷启动清零 + 落位自检 + 读源 + 分发
              + ★一个宏实例化两份扫描（FLASH / ITCM）+ 表填充
  main.c      时钟引导 + 100μs 拍 + 中断外壳(ISR_ITCM 可切) + 运行期选择器
              + 分组统计 + I-cache 对照开关 + PA8/PA9 输出
  syscalls/sysmem.c  newlib 桩
ld/         链接脚本（.itcm_text VMA 0 / .dtcm_shm NOLOAD + 溢出断言）
startup/    ST 启动文件（BSD-3 厂商模板，含 ITCM 拷贝循环）
cmake/      工具链文件
tools/      h723_ports.py（串口自检）
            h723_stage1_read.py（阶段 1：读回 DWT 空拍测量）
            h723_stage2_read.py（阶段 2：单会话 11 组 A/B 测量 + 落位/前提验证 + 守卫）
  legacy/   clock_probe.sh / ws_scan.sh（早期 SWD 探测，已被证伪，留档）
docs/       迁移方案 + 时钟依据 + 频率天花板 + 阶段1报告 + 阶段2报告
            + AUDIT-H723-stage2.md（审计报告）+ REF-flash-placement.md（落位机制实测）+ 硬件接线
```

---

## 常用命令

```bash
bash build.sh                                    # 构建 (ELF 无扩展名 + .bin/.hex/.map)
bash build.sh clean                              # 清理

pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex

python la_tick_freq.py --cpu 400 --ch 4                     # LA 外部测拍频率
python la_tick_freq.py --cpu 400 --ch 4 --rate 16000000      # 看抖动

python tools/h723_stage1_read.py --run 3         # 阶段1: 读回 DWT 空拍测量
python tools/h723_stage2_read.py --dur 0.7       # 阶段2: 完整 11 组 A/B (约 40s)
python tools/h723_stage2_read.py --quick         # 阶段2: 5 组核心对比
python la_tick_freq.py --cpu 156 --ch 1 --rate 1000000 --dur 2.0   # 验证 CH1←PA9 线路

bash sweep_freq.sh 80 90 92 94                   # 频率天花板扫描 (N → 400/450/460/470MHz)

# ★ 用外部仪器测"非默认配置"时: 把配置编进固件 (调试器不参与)
bash build.sh -DDCL_BOOT_PROFILE=1 -DDCL_BOOT_GATE=1 -DDCL_BOOT_SEL=0
python la_tick_freq.py --cpu 400 --ch 4 --rate 16000000 --dur 1.0 --no-reset
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
7. `--gc-sections` 会回收**无人读**的全局（`volatile` 也保不住，
   `__attribute__((used))` 只挡 GCC 层、挡不住链接器）→ 观测变量必须在代码里**真读或真写**
   一次，否则它会从符号表消失（`g_isr_mode` / `g_isr_itcm` 都这么没的）
8. ★ **自检必须"可失败"，否则会被编译器折叠掉** —— 用指针相等做落位自检时，
   GCC 可依据"不同对象地址不同"把整个判断折成常量（实测恒返 0）。
   自检的取值路径必须过 `volatile`，或把权威比对交给外部工具
9. 凡"带长度的读"（`read32 ADDR LEN`）先反测定标语义 —— pyocd 的 LEN 是**字节**
10. 切换测量配置的命令顺序：**先把被测状态设定好 → 再清统计 → 再采样**。
    清统计与设定之间夹进的拍会把 `min` 污染成另一个配置的值
11. ★ **`connect_mode=under-reset` 不带 `-c reset` ⇒ 核心被按在复位态**，
    读回的是**陈旧 SRAM**（症状：tick 冻结、统计量出现垃圾值）。
    每次会话都要 `-c reset -c "sleep 300"` 再读写；判据先读 `DHCSR(0xE000EDF0)`
12. ★ **`la_tick_freq.py` 默认会复位板子** —— 测"运行期写入的配置"必须 `--no-reset`，
    否则仪器测的是骨架态（本项目为此白跑了三次抓取）
13. ★ **pyocd 会话结束后核心被 HALT**，运行期写入的配置不跨会话 ⇒
    要用外部仪器测非默认配置，就把它**编进固件**（`-DBOOT_PROFILE/-DBOOT_GATE/-DBOOT_SEL`）

---

## 与 esp32-core0 的关系

- `esp32-core0`（ESP32-S3）已于 2026-09-10 **冻结：只修不增**；功能增益全部转到本线
- 引擎架构（100μs 硬拍 / 桶化分档 / 原语表 / 四域：连续·逻辑·顺序·通信）是**平台无关**的；
  迁移的是**平台层**（定时器、内存、UART、ADC、GPIO、persist、周期计数）
- 迁移方案与不变量清单：`docs/MIGRATE-H723.md`

---

## 下一步（阶段 3）

1. **桶化分档**（div/phase 三档 + 桶表）—— 现在是"全表扫、不分档"；
   把每拍遍历从 O(全表) 降到 O(本拍激活子集)
2. **deploy 路径**：0x10 写 staging → 热重载 → 生效确认（S3 的
   "ACK=已受理 ≠ 已生效"语义债在 H723 一次到位）
3. **USART1 通信域**（Modbus RTU 从站）—— `SRC_HMI` 目前是留位返回 0
4. **顺序域 Sequencer v0**
5. 把 `tools/` 的 Python 回归脚本从 S3 平移（协议不变 ⇒ 脚本一行不改）
