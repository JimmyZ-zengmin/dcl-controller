# 独立看门狗 (IWDG1) —— 验证记录与机制定案

日期: 2026-09-13  平台: H723 裸机 (`9.10 H723newest`)  作者: 本次会话

---

## 0. 一句话结论

**看门狗已可用并被端到端证明**（注入挂死 → ≤200ms 复位 → `--boot` 说得出是 IWDG1）。
过程中 **-2 的根因被单变量对照实验定案**：不是 LSI、不是寄存器偏移、不是"自己的喂狗打断了
解锁序列"，而是 **`0xCCCC` 必须写在 PR/RLR 之前** —— 看门狗**没启动时，PR/RLR 的更新
永远不会在 VDD 域完成**。

同时发现并修掉 **两个"看起来在工作、其实什么也没测"的判据**（见 §4）。

---

## 1. 现象与排查路径（记录"是怎么走到答案的"）

| 步骤 | 动作 | 结果 |
|---|---|---|
| 1 | 读厂商 CMSIS 头 (`stm32h723xx.h`) | `IWDG1_BASE=0x58004800`、KR/PR/RLR/SR/WINR = 0x00/04/08/0C/10、`RCC_CSR` LSION@0/LSIRDY@1、`RCC_RSR` RMVF@16/IWDG1RSTF@26 —— **与 regs.h 逐位一致**，排除"抄错位域" |
| 2 | 读 ST 的 `stm32h7xx_hal_iwdg.c` | **`HAL_IWDG_Init()` 第一步就是 `__HAL_IWDG_START`**，而整个 H7 HAL **从不**显式开 LSI (注释: "LSI is turned on automatically") |
| 3 | 读 RM0468 §50.3.1 / §50.3.6 / §50.4.4 | 启动语义、写保护语义、更新完成语义（三处原文见 §2） |
| 4 | 现象: `PVU/RVU` 置起后**永不清零** | 值从未落地（读回仍是默认 `PR=0 / RLR=0xFFF`） |
| 5 | 单变量对照实验 | **根因 = 顺序**（见 §3 的 A/B 矩阵） |

★ 排查手段: 本地 RM0468 PDF 无文本抽取库，用 stdlib 写了一个 zlib+正则的抽取器
(`.workbuddy/scratch/pdf_grep.py`)，把 66MB 手册按关键词打印上下文窗口 —— 权威原文可引用，
不必依赖记忆或二手资料。

---

## 2. 权威依据（逐字）

**RM0468 §50.3.1**（启动语义）
> When the independent watchdog is started by writing 0x0000CCCC in IWDG_KR, the counter
> starts counting down from the reset value of 0xFFF.

**RM0468 §50.3.6**（写保护语义）
> Write access to IWDG_PR, IWDG_RLR and IWDG_WINR is protected. To modify them, the user must
> first write 0x00005555 in IWDG_KR. A write access to this register with a different value
> **breaks the sequence** and register access is protected again. This is the case of the
> reload operation (writing 0x0000AAAA).

**RM0468 §50.4.4**（更新完成语义）
> It is reset by hardware when the update operation is completed in the VDD voltage domain
> (takes up to five RC 40 kHz cycles). … after updating the prescaler and/or the reload/window
> value it is **not necessary to wait** until RVU or PVU or WVU is reset before continuing code
> execution except in case of low-power mode entry.

**RM0468 Table 52**（复位源识别）—— 单次事件会置起**多个**标志位
> when an IWDG1 timeout occurs (line #8), both PINRSTF and IWDG1RSTF bits are set, indicating
> that the IWDG1 also generated a pin reset.
> 引脚复位 = CPURSTF+PINRSTF (0x00420000); IWDG1 超时 = IWDG1RSTF+PINRSTF+CPURSTF (0x04420000)

---

## 3. A/B 矩阵（同一套测量方法打出来的读数）

测量面 = `0x22` 按名字读 `WDT_STAT`（**固件自己读回**；本平台 pyocd 读外设区不可靠）。

| # | 构建 | 顺序 | 关闸 | 同步预算 | 读回 PR/RLR/SR | 等更新落 | 结果 |
|---|---|---|---|---|---|---|---|
| 1 | 改前 (v1) | 先配后启动 | 无 | ~15ms | 0 / 0xFFF / 3 | 从未清零 | **rc=−2** |
| 2 | `-DDCL_WDT_START_FIRST=0 -DDCL_WDT_FEED_GATE=0` | **先配后启动** | 无 | **302ms** | 0 / 0xFFF / 3 | **302620µs 跑满预算** | **rc=−2** |
| 3 | `-DDCL_WDT_FEED_GATE=0` | 先启动 | 无 | 100ms | **4 / 99 / 0** | 10093.8µs | rc=0 |
| 4 | 交付 (默认) | 先启动 | 有 | 100ms | 4 / 99 / 0 | 10095 / 10098 / 10101µs | rc=0 |
| 5 | `-DDCL_WDT_PR=6` | 先启动 | 有 | 100ms | 6 / 24 / 0 | **40011.5µs** | rc=0 |

**#2 是决定性的**：#1 与 #3 差了"顺序 + 预算"两件事，单独看判不出。补上 #2（只把顺序退回改前，
预算给到 302ms）后仍然 rc=−2 ⇒ **预算从来不是原因**。

**#2 另有一个收获**：它把 `sync_expired=1`（"跑满预算"）打了出来 —— 证明这个新字段
**能区分"等到了"与"跑满了"**（#4/#5 都是 0）。没有它，"等标志落"是个**空动作**。

### ★ 机制（已验证，不是推断）: 更新耗时 = **5 个预分频后的计数步长**

步长 = `4 × 2^PR / LSI(32kHz)`：

| PR | 步长 | 预测 = 5×步长 | 实测 | 吻合 |
|---|---|---|---|---|
| 4 | 2.0 ms | 10.00 ms | **10.09 ms** | 0.9% |
| 6 | 8.0 ms | 40.00 ms | **40.01 ms** | 0.03% |

旁证（独立量互相印证）: 关闸期间被拦下的喂狗次数 × 100µs = 等待时间
（101 ↔ 10095µs；400 ↔ 40011µs）。

⇒ **PR 越大，初始化阻塞越久**（PR=7 ⇒ 5×16 = 80ms）。换 PR 档必须同步调同步预算。

★ 与手册口径不一致处**如实留档**: §50.4.4 说"≤5 个 RC 40kHz 周期 (≈125µs)"，实测慢 80 倍。
预算按**实测**定，不按标称定。

---

## 4. 顺手修掉的两个"空判据"（都属于"看起来在工作"族）

### 4.1 `上一轮停在 g_stage=?` 恒为 0 —— 取证字段是空的

**机制**: 取证段读 `g_stage`，注释论证"DTCM 跨复位不丢" —— 论证**漏了一环**：
`g_stage` 住在 `.bss`，而**标准启动代码在 `main()` 之前就把它清零了**。
⇒ 该字段永远读 0。实测: 拍 ISR 被注入挂死（上一轮明明已到 stage=7/9），`--boot` 仍报 0。

**修法**: 主循环每轮把 `g_stage / g_tick_count` 写进 AXI 取证记录的 `[30]/[31]`（活体镜像），
取证段从镜像取"上一轮现场"。AXI 是 `(NOLOAD)`、启动不清、跨复位不丢。
**双用途**: 没复位时 = "此刻跑到哪"；复位后 = "上一轮死在哪"。

**验证**: 同一次注入，修后读出 `上一轮: 停在 g_stage=7 / 最后拍=345434`（改前恒 0）。

★ 语义细节（不要误读）: `g_stage` 由 **拍 ISR 每拍写成 7**，所以稳态读数是 **7**（"拍 ISR 在跑"），
不是主循环写的 9。`0` = 没进 main；`1..6` = 死在启动某一阶段；`7` = 拍已跑。
"主循环是否在推进"另由 `g_loop_hb` + `FAULT_LOOP_STALL` 负责（见 wdt.h 的喂狗契约）。

### 4.2 复位原因判据"恰好 1 位" —— **假判据，永远报警**

**机制**: 老 `mgmt.py` 断言"原因位恰好 1 个"。而 RM0468 **Table 52** 明确:
**单次复位事件会置起多个位** —— 引脚复位 = `CPURSTF+PINRSTF` (=0x00420000)。
实测一次正常的复位脚复位就被它判为异常 ⇒ 它**会永远报警**，等于不存在（看门狗复位也会被误判）。

**修法**: 抄 Table 52 的十种**合法签名**逐一对表；命中就报"哪一类复位"，一个都不命中才报警。

**验证**: 引脚复位 0x00420000 → "引脚复位 (NRST)" ✓；IWDG 复位 0x04420000 → "★ 独立看门狗
IWDG1 复位" ✓；改前的累积型 0x01FA0000 → **正确报警** ✓。

---

## 5. 端到端判据（最终交付档，可复跑）

```
注入: pyocd cmd -c reset -c "write32 0x24000400 <15 个 0>" \
      -c "write32 0x24000434 1" -c "write32 0x2400043C 0xF00DBEEF" -c go
      （SD_CFG[13]=1 + 魔数 ⇒ 下一拍 ISR 死循环）
等 ~70s（注入挂死 → 看门狗复位 → 重新启动）
读:   python tools/mgmt.py --boot
```

结果：
```
启动次数 42 → 44         (+2 = pyocd 软复位 + 注入引发的 1 次 IWDG 复位)
复位状态 RSR=0x04420000
复位原因: ★ 独立看门狗 IWDG1 复位   (RM0468 Table52 的合法签名)
上一轮: 停在 g_stage=7 / 最后拍=345434
新的一轮已重新武装: rc=0 / PR=4 / RLR=99 / SR=0 / 等更新落 10101µs / 拦下 101 次
上一轮台账 total=0 sane=OK（干净）
```

**只复位一次**（`SD_CFG[13]` 取走即清，无复位循环）✓

★★ **注入前必须先把 `SD_CFG[0..14]` 整块清零，魔数最后写。**
`SD_CFG` 在 AXI (NOLOAD) 上电=随机；只写魔数会把 `[0..14]` 的随机垃圾**全部放行**成活配置
（`BB_CFG[4]/[5]/[7]`、`SD_CFG[8]/[12]`）。实测第一次没清零 ⇒ 启动异常变慢、行为混乱
（22s / 33s 两档乱跳）。这是 sd.c 里"随机 SRAM 会伪装成'配置没效果'"那条教训的**变体**。

---

## 6. 运行期操作纪律（本次新增，重要）

**★ 看门狗武装之后，任何 ≥200ms 的调试器 HALT 都会导致板子自己复位。**
STM32 的 IWDG 在核被调试器停住时**照常计数**。而 pyocd 的 connect/halt 往往 >200ms
⇒ 会触发一次 IWDG 复位。后果: pyocd 会话"读到的世界"与真实运行态不一致，
且**每次会话都真的把板子复位了一次**（`启动次数` 会涨）。

⇒ 因此（与铁律 0 完全一致）：
* **运行期检查一律走协议**（`mgmt.py --read/--health/--boot`），不要用 pyocd 读 SHM；
* pyocd 只用于**烧录**与**烧录前预写 `SD_CFG`**（那时看门狗还没武装）；
* pyocd 工具收尾必须 `-c go`；
* 已知副作用: pyocd 会话会**静默停掉 `DWT_CYCCNT`** ⇒ 引擎会记 `SCAN_DIV0`
  （本次实测一轮 19376 笔）。**这不是固件缺陷，是调试器污染**，且台账把它抓出来了
  （台账在按设计工作）。

---

## 7. 边界与未做（"宣称 = 实现"）

**看门狗能做的**: CPU + TIM2 + 拍 ISR 活着（喂狗点在拍 ISR）。
**它抓不到的**（必须写明，否则就是宣称 > 实现）:
1. **逻辑锁死但拍照在跑** —— 例: Modbus 接收自锁死那次，拍照正常、isr_cyc 正常，
   看门狗**不会**触发。这类靠故障台账。
2. **输出安全电平** —— 复位只是"停止驱动"。真安全电平要靠复位后尽早显式驱动
   （boot 安全段）+ 硬件侧**外部下拉**（尚未加）。
3. **确定性退化**（周期变长但没卡死）—— 靠 `isr_cyc` / 预算判据。

**未做 / 待做**:
* `-DDCL_WDT=0` 构建 + 注入 ⇒ 应**永久卡死**（负向对照，只有一半做完了: 正向已验）。
* 长稳 ≥8h 与 I/O 域故障注入。
* PR=7 档未验（按 5 步推断应 ~80ms，超出现有 100ms 预算边缘）。
* **启动到管理面可用需 9s / 33.5s 两档**（SD 初始化那一段），对 PLC 是成熟度问题:
  为什么会有两档、能否缩短，未查。工具 `tools/wait_board.py` 先把"等"这件事变成动作。
* 外部下拉（真失效安全输出）。

---

## 8. 复跑方法

```bash
# 交付构建
bash build.sh                     # 打印生效开关 (含 DCL_WDT* 全部)
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
pyocd cmd -t stm32h723xx -O connect_mode=under-reset -c reset -c go
python tools/wait_board.py --timeout 70          # ★ 复位后别急着读, 板子要 ~10-35s 才应答

# 归因/取证
python tools/mgmt.py --boot                     # 复位原因(Table52) + 上一轮现场 + 活体镜像
python tools/mgmt.py --read WDT_STAT            # 武装结果 + PR/RLR/SR 读回 + 顺序/关闸/预算取证
python tools/mgmt.py --health                   # 全套健检

# 对照构建 (三份, 用同一套读数判读)
bash build.sh -DDCL_WDT_START_FIRST=0 -DDCL_WDT_FEED_GATE=0   # ⇒ 应 rc=-2, sync_expired=1
bash build.sh -DDCL_WDT_FEED_GATE=0                           # ⇒ 应 rc=0 (关闸不是根因)
bash build.sh -DDCL_WDT_PR=6                                  # ⇒ 等更新落应 ≈40ms
```
