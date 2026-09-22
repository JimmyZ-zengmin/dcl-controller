# CHANGELOG — 运动控制路径搬进拍内（2026-09-21）

> 类型：**①层改动**（改变 ISR 可达代码与每拍行为）⇒ 按纪律必走 A/B + 全量回归。
> 交付档指纹：`c2ca850bbb019166e3d2d0821b175574` → **`4a6993bfbbde17d8ab4f48d63e51c245`**

---

## 1. 缺陷：有效控制周期是 38.7 ms，不是 100 µs

`step_service_motion()`（运动下发）与 `step_tick()`（到点自停 / 斜坡推进 / 限时截止）
**都在主循环里被调用**（`src/main.c:4718-4719`，主循环体内）。

⇒ 闭环的动作链实际是：

```
拍内 (10 kHz)     I2C 采集 → SENSOR[] → 引擎扫描(PID…) → 写 wire[12..15]
                          ↓  ★ 在这里断掉
主循环 (≈2.7 kHz)  step_service_motion() 读 wire[] → step_set_rate/dir/ena/deadline
                  step_tick()            斜坡推进 g_step_rate_out += dv、限时截止
                          ↓
硬件 (TIM3)       ARR ← g_step_rate_out ⇒ 脉冲序列
```

**"环在片内每拍跑"这句话对"算"成立，对"执行"不成立。**

### 证据（不靠推理，靠实测）

| 来源 | 内容 |
|---|---|
| 上机实测 | `g_step_dt_max` = **387 拍 = 38 700 µs**（`0x39 op=19` 应答的 `+64`） |
| 源码注释 | `src/step.c` 的 `DCL_STEP_RAMP_FIX` 段：「本函数**由主循环每 ~0.37 ms（≈3.7 拍）**调一次」 |
| 那次的倍率 | `×2.7` 缺陷的倍率 = `1 ms ÷ 0.37 ms = 2.70` —— **如果斜坡在拍内推进，这个缺陷不可能存在** |
| 审计覆盖 | `docs/PLAN-io-into-engine.md`（"把主循环上的 I/O 搬进拍内"）**全文 "step" 出现 0 次** ⇒ 从未覆盖 step 模块 |
| 文档冲突 | `docs/SUPPORTED-SCOPE.md` 原写「主循环一圈 ≈ 0.37 ms **不进控制回路**」；而 `main.c:2694` 自己写着「到点由 `step_tick`（**主循环**）停脉冲」 |

---

## 2. 改了什么（**最小侵入**：函数体一行未改，只改"从哪儿调"）

| 文件 | 改动 |
|---|---|
| `src/step.c` | `step_tick` **拆为两个**：<br>· `step_tick_isr()` = 到点自停 + 斜坡推进 + 限时截止 → **拍内**（`DCL_ITCM`）<br>· `step_diag_tick()` = PE9「指令 vs 引脚实读」核对 → **留主循环**<br>+ `#include "itcm.h"` + **10 处 `DCL_ITCM`** |
| `src/step.h` | 两个函数声明 + 动机注释；`step_tick` 旧声明移除 |
| `src/main.c` | ISR 里调用（见下"位置"）；主循环只留 `step_diag_tick` + 对照档分支 |
| `tools/h723_restore_delivery.sh` | `EXPECT_MD5` 更新（**不同步会让恢复路径死掉**，P32） |

`step_service_motion()` 加 `DCL_ITCM`，改由拍内调用 —— **函数体未改**。

### ★ 调用位置不是随便选的

```
… engine 扫描（写 wire[]） …
ISR_CKPT(5);
  step_service_motion();          ← 本拍新值，且
  step_tick_isr(g_tick_count);    ← 必须在 do_poll 之前
do_poll(g_shm, g_tick_count);     ← 读 ACTUATOR → 写 GPIOE_ODR
```

- **在 engine 之后**：读到的 `wire[12..15]` 才是本拍的新值。
- **在 `do_poll` 之前**：`step_set_ena()` 写 `ACTUATOR[9]`，而 `do_poll` 才把它搬到
  `GPIOE_ODR`。**顺序反了，ENA 要等下一拍才到引脚。**
- **在 `hil_out_poll` 之后**：两者共享 `TIM3_CH1` ⇒ 让运动面（主应用）优先。

### ★ A/B 复用既有 `IO_IN_ISR`，不新增开关

`-DDCL_IO_IN_ISR=0` = **改前行为**（运动也由主循环驱动）—— 语义本来就是"I/O 驱动位置"。

---

## 3. 闸门替人抓出了传递闭包

第一次构建只补了 8 个 `DCL_ITCM`，`tools/gate_isr_itcm.py` 精确报出还差两个：

```
★ 违规: 以下函数**从 ISR 可达, 却落在 FLASH** (2 个):
    apply_rest_ena           0x0800ACB8
    step_rate_apply          0x0800AD38
  ⇒ 加 `DCL_ITCM`。**注意传递闭包**: 只补直接被调者不够。
```

⇒ 补上后过闸。**ITCM 占用 13 636 → 15 276 B，到向量表仍余 75.1%。**

★ 这条正是"**光有工具不进闸门 = 没做**"的正面例子：闭包靠人眼很难补全，闸门一次就说清了。

---

## 4. 验收

### 4.1 A/B 判据（上机实测）

| 构建 | `g_step_dt_max` |
|---|---|
| `IO_IN_ISR=0`（对照 = 改前） | **387 拍 = 38 700 µs** |
| `IO_IN_ISR=1`（**交付**） | **1 拍 = 100 µs**（连测 3 次稳定） |

**⇒ `dt` 恒为 1 ⇒ 每拍确实在跑。387× 改善。**

### 4.2 功能验证（不能只看判据 —— 电机还得会转）

| 判据 | 结果 |
|---|---|
| M1 时基 | **PASS**（`pmin` 非 0） |
| M2 使能后 PE9 **引脚实读** | **PASS**（`PE9=1` / `CC1E=1`） |
| M4 真位移 | **PASS**（`SENSOR[0]` 变化 3 084 counts） |
| R 停后不动 | **PASS**（Δ = 0.0 LSB） |
| ★ **三角波连续采 `ARR`** | **450 ~ 3012 Hz**（设计 0.15A=450、A=3000）⇒ **PASS** |

### 4.3 构建闸门

**六道闸门全过**（零警告 / ISR 调用树 / 应答缓冲 / ③层静态 / 契据可机检 / 判据自测）。

---

## 5. ❌ 未完成（如实登记）

| # | 项 | 状态 / 原因 |
|---|---|---|
| 1 | **12 套全量回归** | ⛔ **被前置闸门拦下**：`0x49 PROG_ERASE -> NAK erase failed`；`SD_DIAG` 显示 `stage=2 err=0xBAD1 容量=0` ⇒ **SD 卡不在位**（被抽去读数据了）。<br>★ 闸门**正确地判"无效"而不是"失败"** —— 那些红不是回归。<br>**⇒ 卡插回后必须补跑。** |
| 2 | ② TX 环 SPSC + 每拍一字节 | 未做。**按"一次只动一个①层变量"的纪律单独一轮**（判据：指针所有权源码级检查 + 应答延迟上界） |
| 3 | 200 µs 档重建 | 未做（指纹只更新了 100 µs 档） |

---

## 6. 顺带修正的文档（本次一并改）

| 文档 | 原内容 | 改成 |
|---|---|---|
| `docs/SUPPORTED-SCOPE.md` | 「主循环一圈 ≈ 0.37 ms **不进控制回路**」 | 保留结论，但**显式标注"这一句是修复之后才成立的；之前不成立"**，并给出 38.7 ms 的实测与修复位置 |
| `docs/ARCH-H723.md` §8.5 | 把 `ai/di/hil_tick` 写在主循环（**那是对照档**）、**完全漏了 `step_*`** | 按 `src/main.c` 实际调用点**逐行重写**，补 `step_*` 三行 + 加 A/B 判据表 |

★ 这两处都是**"同一事实在两处说法不同"**族的实例（本项目反复踩的那一类）。
发现它们靠的是**把代码调用点与文档逐条对**，不是靠读文档。

---

## 7. 复跑

```bash
# A/B（对照片）
bash build.sh -DDCL_IO_IN_ISR=0
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
# 读 0x39 op=19 应答的 +64  ⇒ 应回到 ~387

# 交付档
bash build.sh                      # 全默认（IO_IN_ISR=1）
pyocd flash -t stm32h723xx -O connect_mode=under-reset build/dcl_h723.hex
# 读 +64 ⇒ 应为 1

# 全量回归（★ 需要 SD 卡在位）
bash tools/h723_full_regress.sh
```
