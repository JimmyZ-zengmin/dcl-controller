# DCL — 确定性控制运行时（STM32H723 / Cortex-M7）

**一个用「静态路由表」替代「程序」的裸机控制运行时。**
没有 RTOS、没有调度器、没有 `while(1)` 大循环。全系统只有一个 **100 µs 的硬件定时硬拍**；
拍内按拓扑序扫描一张**编译期生成的静态路由表**，逐条完成「读源 → 算原语 → 写目标」，结果直接落到
GPIO / PWM / 通信。表里没有动态分支、热代码全部住在零等待 ITCM，**每拍花多少周期是编译期就能算出的量**。

**一次烧录，控制逻辑可以反复下发覆盖 —— 热更新 ≤ 1 拍。**

---

## 📌 交付入口（先读这两页）

| 你要做什么 | 读这页 |
|---|---|
| **我用它到底能靠什么、不能靠什么**（可信范围 + 使用入口 + 遇到问题怎么救）| **[`docs/SUPPORTED-SCOPE.md`](docs/SUPPORTED-SCOPE.md)** |
| **这一版新增/变了什么、哪些已知问题还没解决**、**每个数字的复跑命令** | **[`docs/RELEASE-v2.1.0.md`](docs/RELEASE-v2.1.0.md)** |
| 从零跑通（构建 / 烧录 / 第一个程序）| [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) |
| 需求"应该是什么"（契据）| [`docs/REF-program-contract.md`](docs/REF-program-contract.md) |

**版本**：`v2.1.0`（基线 `v2.0.0`）｜**能力字**：`0x7DF7`｜**构建闸门**：6 道｜**上机判据**：126 条

---

## 30 秒：它能干什么

| | 一句话 | 怎么自己验 |
|---|---|---|
| **接一个新器件 = 上传一段配置** | 不用改固件、不用重烧：下发"每 N 拍读 设备/寄存器/长度 → 写 `SENSOR[z]`" | `python tools/h723_dev_bind_test.py --port COMxx` → **52/0/1** |
| **配置随程序包持久化** | 上传程序包时自动带上绑定表，**开机自动恢复**；老包行为逐字节不变 | `python tools/h723_devbind_persist_test.py --port COMxx` → **32/0** |
| **应答带归属** | 应答回显 `CMD+SEQ`，客户端能判"这条是不是我的"（不再吃错帧）| `python tools/h723_frame_attrib_test.py --port COMxx` → **18/0** |

```bash
bash build.sh                                       # ① 构建（6 道闸门，任一不过即失败）
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex   # ② 烧录
python tools/h723_proto.py --port COMxx             # ③ 探活 → 预期 12/0
bash tools/h723_full_regress.sh                     # ④ 一键全量回归（20+ 套件串行 + 末尾探活）
```

★ **认口按能力字**（不要认"第一个 CH340"），且判据用**必备位掩码** `(cap & 0x0DF7) == 0x0DF7`
—— 能力字随版本增长（`0x0DF7 → 0x1DF7 → 0x3DF7 → 0x7DF7`），**等值判据会让你以为"板子没响应"**。
★ **串口是独占资源**：同一时刻只允许一个进程用（并发会让两边都读到串帧）。

---

## 它解决什么问题（为什么不是又做一个 PLC）

工业场景里拿"确定性"，主流两条路：

| 路线 | 怎么拿确定性 | 代价 |
|---|---|---|
| **PLC / 软 PLC** | 把周期拉到 ms 级，用长周期"藏住"抖动 | 响应慢；要做 µs 级闭环就得换路线 |
| **本运行时** | **不藏**：拍固定 100 µs，**每拍成本编译期可算**，热代码在 ITCM | 需要"编译期就确定"的设计纪律 |

**它靠三件事做到**：① 硬拍驱动（TIM2，实测抖动 σ 在**个位数 ns**）·
② 静态拓扑 + 零等待 ITCM（无 cache 不确定性）· ③ 通信下发 + SD A/B 持久化（改逻辑不重烧）。

**它明确不是什么**（详见 [`SUPPORTED-SCOPE.md`](docs/SUPPORTED-SCOPE.md) §3）：
不是实时 Linux / 不是通用 PLC 替代品 / **不承诺"发请求即得值"**（事务跨拍，必须用就绪门）/
**不承诺内部 flash 掉电保持**（已按设计降级 ⇒ 持久化一律走 SD）/
**不承诺"输出由硬件锚定"**（该说法已被实测推翻：引脚时刻 = CPU 最后一次写 + 0.12 µs）。

---

## ★★ 铁律 0 —— 非侵入式交互（**先读这条**）

> **观测不得改变被测对象。** 与单片机交互/观测时**优先走协议内通道**；
> 凡会改变目标状态的手段（HALT、复位、关时钟、改 DWT、留运行期配置），**只在协议通道表达不了时才用**，
> 且用完必须**显式恢复**并**核对已恢复**。

**为什么排第 0**：它的代价是"**静默的假故障**" —— 观测动作把目标推进到另一个状态，
而症状看起来像"固件坏了 / 新代码写错了"，**排查方向会被整轮带偏**。
本项目有 6 条纪律都是它的个案（例：`pyocd` 会话会**静默停掉 `DWT_CYCCNT`**，于是所有计时量读 0 而其余一切正常）。
完整血证见 [`docs/README-full-legacy.md`](docs/README-full-legacy.md) 的「铁律 0」节。

---

## 硬件与接线

| 项 | 说明 |
|---|---|
| 板 | 鹿小班 **LXB723ZG-P1**（STM32H723ZGT6，1 MB Flash） |
| 晶振 | **HSE 25 MHz**（板上无源晶振，已实测反推确认） |
| 调试 | DAPLink（CMSIS-DAP）→ `pyocd`，目标名 **`stm32h723xx`** |
| 逻辑分析仪 | Saleae Logic2，接线 **`CH4 ← PA8`**（拍输出），GND 共地 |

**串口（PC 直连，板子 H1 排针）**

| CH340 | H1 脚 | 信号 |
|---|---|---|
| RXD | **6** | PA9 = USART1_TX |
| TXD | **5** | PA10 = USART1_RX |
| GND | 任一 GND | 共地（**必接**）|

★ 时钟初始化失败时固件会在 **PA8 上闪 `|错误码|` 次**（错误码见 `src/clock.h` 的 `CLK_ERR_*`）
⇒ 不依赖 SWD 也能定位卡在哪一步。

---

## 一条命令查全部：`bash tools/h723_full_regress.sh`

20+ 套件串行跑、独占串口、**末尾自动探活**；文件头写清了**看什么**
（"**有没有出现新的失败模式**"，不是"PASS 数不低于某值"）与**哪些失败是设计内的**
（内部 flash 持久化已降级 / modbus 需 485 回路 / w5 的 HIL 输出臂需接线 / 若干已知测试缺陷）。
端口可用 `DCL_PORT=COM7 bash tools/h723_full_regress.sh` 覆盖。

---

## 文档地图（**每份文档是哪个问题的权威源**）

| 你要问 | 权威源 |
|---|---|
| 我能靠什么 / 不能靠什么、入口怎么用、遇错怎么救 | [`docs/SUPPORTED-SCOPE.md`](docs/SUPPORTED-SCOPE.md) |
| 这一版新增了什么 / 破坏性变更 / **已知问题** / 证据与复跑 | [`docs/RELEASE-v2.1.0.md`](docs/RELEASE-v2.1.0.md) |
| 需求"应该是什么"（**契据**）| [`docs/REF-program-contract.md`](docs/REF-program-contract.md) |
| 契据里**哪几条能被机器验** | [`docs/claims.md`](docs/claims.md)（构建时强制一致）|
| 引擎内部怎么运作（四公理 + 八不变量）| [`docs/CORE-ENGINE.md`](docs/CORE-ENGINE.md) |
| 架构 / 命令表 / 内存地图 | [`docs/ARCH-H723.md`](docs/ARCH-H723.md) · [`docs/MEMORY-LAYOUT.md`](docs/MEMORY-LAYOUT.md)（★ **SHM 偏移以 `src/engine.h` 为唯一权威**）|
| 最近发生了什么、怎么定位到的 | [`docs/STATUS-2026-09-16.md`](docs/STATUS-2026-09-16.md) |
| 历史长篇（含逐日流水、铁律 0 血证）| [`docs/README-full-legacy.md`](docs/README-full-legacy.md) |

---

## 怎么保证"能信"（本系统最值得看的部分）

1. **构建 6 道闸门**（任一不过即失败）：零警告 · ISR 调用树（ISR 可达 ⇒ 必须住 ITCM）·
   应答缓冲区越界（静态）· ③层静态判据（程序不得出现引脚号/地址/寄存器名）·
   **契据可机检**（8 类主张：结构 / 拒绝码 / 能力位 / 留位 / 完备性 / 文档漂移 / 判据存在性 / **门面一致性**）。
2. **每条判据都能失败**：配了变异体（注入固件缺陷看判据是否变红）· 反空判据 · 逐字节比对。
3. **"覆盖不到"判 SKIP，不是 PASS**；闸门"覆盖不足"判**无效**，不是"干净"。
4. **未覆盖项必须显式声明**（`--allow-uncovered <理由>`）—— 不允许沉默地留着。
5. **改契据/门面而不同步 `docs/claims.md` ⇒ 构建会红** —— 这一步把"忘了同步"
   从"下一个人照错的做"变成"构建过不去"。

---

## 目录结构（阅读顺序）

```
src/          引擎核心（engine / isr / transport / dev_bind / i2c_sm / prog_store / sd / blackbox …）
tools/        验收与工具（每个结论都能用这里的脚本复跑）+ h723_full_regress.sh
docs/         契据 / 发布说明 / 可信范围 / 入门 / 架构 / 内存地图 / 状态 / 审计
examples/     ③层 DCL 示例程序（**不得出现引脚号**，构建期静态判据会拒绝）
ld/ startup/  链接脚本与启动文件（显式定义 DTCM / ITCM 段）
build.sh      构建（含 6 道闸门）
```

---

## 许可与状态

内部项目（`JimmyZ-zengmin/dcl-controller`）。**未解决的问题逐条列在**
[`docs/RELEASE-v2.1.0.md`](docs/RELEASE-v2.1.0.md) §6 —— 请在使用前读一遍，
那是"我知道它哪里不行"的完整清单。
