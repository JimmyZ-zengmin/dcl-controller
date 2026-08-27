# DCL 语言规范 v1.0

> Deterministic Control Language — 确定性控制语言  
> 目标硬件: STM32H723ZG (LQFP144)  
> 固件版本: 核心0 v1.9  
> 编译器: dcl_compiler_v1.py  
> 文档日期: 2026-07-11

---

## 1. DCL 是什么

DCL 是一种**声明式数据流语言**，专门为确定性实时控制设计。

### 1.1 核心理念

| 传统 PLC 语言 | DCL |
|--------------|-----|
| 命令式 (IF/THEN/ELSE, FOR) | **声明式** (描述"是什么"，不是"怎么做") |
| 程序员编排执行顺序 | **编译器自动拓扑排序** |
| 变量有类型 (INT/REAL/BOOL) | **所有信号都是 float32**，统一 WIRE 总线 |
| 程序 = 可执行代码 | **程序 = 路由表 (数据结构)** |
| 解释执行 (扫描周期 1-10ms) | **确定性执行引擎 (100μs, <7ns抖动)** |

### 1.2 一句话概括

> DCL 的每一行代码定义一个**功能块实例**及其**输入输出连接**。  
> 编译器自动推断执行顺序。MCU 不解释程序，只按路由表依次执行原语。

---

## 2. 语言结构

### 2.1 程序结构

一个 DCL 程序 = 多行 FB 声明，**无入口点、无函数定义、无控制流**。

```
# 这是注释
// 这也是注释
/* 这是
   多行注释 */

SENSOR  temp      FROM ADC1_CH0
FILTER  temp_f    FROM temp        LOWPASS a=0.1
PID     heater    FROM temp_f      SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
ALARM   overheat  FROM temp_f > 90
OUTPUT  heat_pwm  TO TIM1_CH1      FROM heater
```

### 2.2 命名规范

| 类型 | 规则 | 示例 |
|------|------|------|
| FB 关键字 | 全大写 | `SENSOR`, `PID`, `FILTER` |
| 信号名 | 小写字母+数字+下划线 | `temp`, `temp_f`, `heater_pid` |
| 硬件源 | 全大写+下划线 | `ADC1_CH0`, `TIM1_CH1`, `GPIO_PE5` |
| 参数键 | 大写+等号 | `SP=60`, `KP=2.0`, `PT=3s` |
| 输出箭头 | `→` (Unicode) | `→ Q=motor_on` |

### 2.3 数据模型

```
所有信号 = WIRE 总线上的 float32 值

WIRE[0] = 25.30    ← SENSOR temp 的值
WIRE[1] = 24.80    ← FILTER temp_f 的值
WIRE[2] = 45.20    ← PID heater_pid 的中间值
WIRE[3] = 45.20    ← CLAMP heater 的输出值
WIRE[4] = 0.00     ← CMP overheat 的值
WIRE[5] = 45.20    ← DIRECT out_heat_pwm 的值
```

- 信号名只在编译期存在，运行时只有 WIRE 索引
- 每个 WIRE 被恰好一个 FB 写入，可被多个 FB 读取
- WIRE 总线容量: 1024 个 float32

---

## 3. 功能块 (FB) 完整参考

### 3.1 SENSOR — 硬件输入

```
SENSOR <name> FROM <source> [SCALE <k> <b>] [RANGE <lo> <hi>]
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 信号名，分配到 WIRE |
| source | ✅ | 硬件源: ADC1_CH0, GPIO_PE5 等 |
| SCALE k b | ❌ | 线性标定: y = k×x + b (生成 SCALE 原语) |
| RANGE lo hi | ❌ | 等价于 SCALE hi-lo lo |

**编译行为:**
- 无 SCALE/RANGE → 生成 `DIRECT` 原语 (直接拷贝)
- 有 SCALE/RANGE → 生成 `SCALE` 原语

**示例:**
```
SENSOR temp FROM ADC1_CH0              # 直接读取
SENSOR pressure FROM ADC1_CH1 SCALE 0.01 0.0   # y = 0.01x
SENSOR voltage FROM ADC1_CH2 RANGE 0 3.3        # y = 3.3x
```

**生成路由数:** 1

---

### 3.2 FILTER — 低通滤波

```
FILTER <name> FROM <signal> LOWPASS a=<alpha>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 输出信号名 |
| signal | ✅ | 输入信号名 |
| a=alpha | ✅ | 滤波系数 (0~1), 越小越平滑 |

**算法:** `y += alpha × (x - y)` — 一阶 IIR 低通滤波

**状态:** 有状态 (1 个 state slot，保存上一次输出 y)

**示例:**
```
FILTER temp_f FROM temp LOWPASS a=0.1    # α=0.1, 强滤波
FILTER fast_f FROM sensor LOWPASS a=0.5  # α=0.5, 轻滤波
```

**生成路由数:** 1

---

### 3.3 PID — PID 控制器

```
PID <name> FROM <signal> SP=<setpoint> KP=<kp> KI=<ki> KD=<kd> [LIMIT <lo> <hi>]
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 输出信号名 (限幅后) |
| signal | ✅ | 过程变量 (PV) 输入 |
| SP | ✅ | 设定值 |
| KP | ✅ | 比例增益 |
| KI | ✅ | 积分增益 |
| KD | ✅ | 微分增益 |
| LIMIT lo hi | ❌ | 输出限幅 (默认 0~100) |

**编译行为:**
- 生成 2 条路由: `PID` 计算 + `CLAMP` 限幅
- 自动创建中间 WIRE: `{name}_pid` (PID 原始输出) 和 `{name}` (限幅后输出)

**状态:** 有状态 (1 个 state slot，保存积分项和上一次误差)

**示例:**
```
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
```

**生成路由数:** 2  
**生成参数块:** 2 (PID参数 + CLAMP参数)

---

### 3.4 ALARM — 比较报警

```
ALARM <name> FROM <signal> <op> <threshold>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 报警信号名 (1.0=触发, 0.0=正常) |
| signal | ✅ | 输入信号 |
| op | ✅ | 比较运算符: `>`, `>=`, `<`, `<=` |
| threshold | ✅ | 阈值 (float) |

**编译行为:** 生成 `CMP` 原语

**示例:**
```
ALARM overheat FROM temp_f > 90       # 温度超限
ALARM low_press FROM pressure <= 0.5  # 低压报警
```

**生成路由数:** 1

---

### 3.5 LOGIC — 逻辑运算

```
LOGIC <name> = <signal1> <op> <signal2>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 输出信号名 |
| signal1 | ✅ | 输入信号 A |
| op | ✅ | `AND`, `OR`, `NOT` |
| signal2 | ✅ | 输入信号 B (NOT 时也为信号名) |

**编译行为:**
- `AND` → OP_AND (0x0F): 两个输入都 > 0.5 则输出 1.0
- `OR` → OP_OR (0x10): 任一输入 > 0.5 则输出 1.0
- `NOT` → OP_NOT (0x11): 输入取反

**示例:**
```
LOGIC fault = overheat OR undertemp
LOGIC safe = fault AND NOT override
```

**生成路由数:** 1

---

### 3.6 TIMER — 定时器

```
TIMER <name>: IN=<signal>, PT=<time><unit> [, mode=<TON|TOF|TP>] → Q=<output> [, ET=<elapsed>]
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 定时器实例名 |
| IN | ✅ | 输入信号 (触发条件) |
| PT | ✅ | 预设时间 (带单位: s 或 ms) |
| mode | ❌ | TON (默认, 延时接通), TOF (延时断开), TP (脉冲) |
| Q | ✅ | 输出信号 |
| ET | ❌ | 经过时间信号 |

**IEC 61131-3 对应:**
- TON: IN=1 开始计时，到达 PT 后 Q=1
- TOF: IN=1→Q=1，IN=0 开始计时，到达 PT 后 Q=0
- TP: IN 上升沿触发固定宽度脉冲

**示例:**
```
TIMER t1: IN=btn, PT=3s → Q=motor_on          # TON 3秒
TIMER delay: IN=start, PT=500ms, mode=TOF → Q=hold  # TOF 500ms
TIMER pulse: IN=trigger, PT=100ms, mode=TP → Q=flash  # TP 100ms脉冲
TIMER t2: IN=switch, PT=2s → Q=ready, ET=elapsed  # 带经过时间
```

**生成路由数:** 1 (无 ET) 或 2 (有 ET)

---

### 3.7 COUNTER — 计数器

```
COUNTER <name>: CU=<signal>, PV=<preset> → Q=<output>, CV=<count>
COUNTER <name>: CD=<signal>, PV=<preset> → Q=<output>, CV=<count>
COUNTER <name>: CU=<signal>, CD=<signal> [, PV=<preset>] → Q=<output>, CV=<count>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| CU | ✅ (CTU/CTUD) | 加计数输入 |
| CD | ✅ (CTD/CTUD) | 减计数输入 |
| PV | ❌ | 预设值 (默认 100) |
| Q | ✅ | 到达预设值输出 |
| CV | ✅ | 当前计数值输出 |

**IEC 61131-3 对应:**
- CTU: 加计数，CU 上升沿 CV+1，CV≥PV 时 Q=1
- CTD: 减计数，CD 上升沿 CV-1，CV≤0 时 Q=1
- CTUD: 加减计数，同时支持 CU 和 CD

**示例:**
```
COUNTER parts: CU=part_detected, PV=100 → Q=batch_done, CV=parts_count   # CTU
COUNTER countdown: CD=tick, PV=60 → Q=timeout, CV=remaining              # CTD
COUNTER bidir: CU=up_cmd, CD=down_cmd, PV=50 → Q=at_limit, CV=pos       # CTUD
```

**生成路由数:** 2 (CV 路由 + QU 路由)

---

### 3.8 LATCH — 锁存器 (SR 触发器)

```
LATCH <name>: S1=<set_signal>, R=<reset_signal> → Q1=<output>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| S1 | ✅ | 置位输入 (优先) |
| R | ✅ | 复位输入 |
| Q1 | ✅ | 输出 |

**编译行为:** 生成 `SR` 原语 (Set-dominant: S 和 R 同时为 1 时，Q=1)

**示例:**
```
LATCH motor_run: S1=start_btn, R=stop_btn → Q1=running
```

**生成路由数:** 1

---

### 3.9 SCALE — 线性标定

```
SCALE <name> FROM <signal> RANGE <lo> <hi>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 输出信号名 |
| signal | ✅ | 输入信号 |
| lo hi | ✅ | 输出范围下限和上限 |

**算法:** `y = (hi - lo) × x + lo`

**示例:**
```
SCALE percent FROM raw RANGE 0 100       # 归一化到百分比
SCALE celsius FROM adc_val RANGE 0 150   # 映射到温度范围
```

**生成路由数:** 1

---

### 3.10 RATE — 变化率 (微分)

```
RATE <name> FROM <signal>
```

**算法:** `y = (x_current - x_previous) / dt` — 当前值与上一周期值之差

**状态:** 有状态 (保存上一周期值)

**示例:**
```
RATE temp_rate FROM temp_f    # 温度变化率
```

**生成路由数:** 1

---

### 3.11 DEADBAND — 死区滤波

```
DEADBAND <name> FROM <signal>, <width>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 输出信号名 |
| signal | ✅ | 输入信号 |
| width | ✅ | 死区宽度 |

**算法:** |x| < width → y = 0, 否则 y = x

**状态:** 有状态

**示例:**
```
DEADBAND clean FROM noisy, 0.5    # 消除±0.5以内的小波动
```

**生成路由数:** 1

---

### 3.12 OUTPUT — 硬件输出

```
OUTPUT <name> TO <target> FROM <signal>
```

| 参数 | 必须 | 说明 |
|------|------|------|
| name | ✅ | 输出实例名 |
| target | ✅ | 硬件目标: TIM1_CH1, GPIO_PE5 等 |
| signal | ✅ | 输入信号名 |

**编译行为:** 生成 `DIRECT` 原语，自动创建 `out_{name}` 中间 WIRE

**示例:**
```
OUTPUT heat_pwm TO TIM1_CH1 FROM heater      # PWM 输出
OUTPUT fault_led TO GPIO_PE5 FROM fault      # 数字输出
```

**生成路由数:** 1

---

## 4. 编译过程

### 4.1 编译流程

```
DCL 源代码
    │
    ├── 1. 预处理: 去除注释 (# 和 // 和 /* */)
    │
    ├── 2. 逐行解析: 匹配 FB 关键字 → 分配 WIRE/参数/状态
    │      每行生成 1~2 条路由 (RouteEntry)
    │
    ├── 3. 拓扑排序: Kahn 算法按 WIRE 依赖关系排序路由
    │      保证: 读取 WIRE[i] 的路由一定在写入 WIRE[i] 的路由之后
    │      检测: 循环依赖 → 编译错误
    │
    ├── 4. 资源验证: 路由≤1024, 参数≤512, 状态≤256, WIRE≤1024
    │
    └── 5. 生成二进制: 路由表 + 参数表 + CRC32 校验
```

### 4.2 路由表二进制格式

```
┌──────────────────────────────────────────────────────────┐
│ Header (12 bytes)                                         │
│   route_count:    uint32 LE    # 实际路由数               │
│   param_count:    uint32 LE    # 参数表使用数             │
│   active_routes:  uint32 LE    # = route_count            │
├──────────────────────────────────────────────────────────┤
│ Route Table (1024 × 16 bytes = 16384 bytes)              │
│   每个 RouteEntry:                                        │
│     [0] src_type:    uint8  (0=SENSOR, 1=WIRE, 2=CONST)  │
│     [1] src_index:   uint8  (源 WIRE 索引)               │
│     [2] dst_type:    uint8  (3=WIRE)                      │
│     [3] dst_channel: uint8  (目标 WIRE 索引)             │
│     [4] op:          uint8  (原语操作码)                  │
│     [5] flags:       uint8  (1=有效)                      │
│     [6] param_idx:   uint16 LE (参数表索引)               │
│     [7] state_offset:uint16 LE (状态表偏移)               │
│     [8] actuator_idx:uint16 LE (执行器索引)               │
│     [9] wire2_idx:   uint16 LE (第二输入 WIRE 索引)       │
├──────────────────────────────────────────────────────────┤
│ Param Table (512 × 16 bytes = 8192 bytes)                │
│   每个参数: value_a, value_b, value_c, value_d (4×float) │
├──────────────────────────────────────────────────────────┤
│ CRC32 (4 bytes, big-endian)                               │
└──────────────────────────────────────────────────────────┘
总大小 = 12 + 16384 + 8192 + 4 = 24592 bytes (固定)
```

### 4.3 一条 DCL 语句 → 多条路由的例子

```
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100

编译结果:
  WIRE[1] → OP_PID → WIRE[2]  (PID计算, 输出 heater_pid)
  WIRE[2] → OP_CLAMP → WIRE[3]  (限幅, 输出 heater)

  参数表[0] = {kp=2.0, ki=0.1, kd=0.05, sp=60.0}  (PID参数)
  参数表[1] = {lo=0.0, hi=100.0, 0, 0}            (CLAMP参数)
  状态表[0] = heater 的积分项+上次误差
```

---

## 5. 原语操作码全表

| 操作码 | 十六进制 | 有状态 | 说明 |
|--------|---------|--------|------|
| DIRECT | 0x00 | ❌ | 直接传递 |
| CMP | 0x01 | ❌ | 比较 (>/</>=/<=) |
| HYST | 0x02 | ✅ | 迟滞比较 |
| CLAMP | 0x03 | ❌ | 限幅 |
| LPF | 0x04 | ✅ | 低通滤波 |
| PID | 0x05 | ✅ | PID 控制器 |
| RATE | 0x06 | ✅ | 变化率 (微分) |
| DEADBAND | 0x07 | ✅ | 死区滤波 |
| MUX | 0x08 | ❌ | 多路选择 |
| EDGE | 0x09 | ✅ | 边沿检测 |
| LUT | 0x0A | ❌ | 查找表 |
| CNT | 0x0B | ✅ | 计数 (底层) |
| TIMER | 0x0C | ✅ | 定时器 |
| SCALE | 0x0E | ❌ | 线性标定 y=kx+b |
| AND | 0x0F | ❌ | 逻辑与 |
| OR | 0x10 | ❌ | 逻辑或 |
| NOT | 0x11 | ❌ | 逻辑非 |
| REG | 0x12 | ✅ | 寄存器 (锁存) |
| ADD | 0x13 | ❌ | 加法 |
| SUB | 0x14 | ❌ | 减法 |
| MUL | 0x15 | ❌ | 乘法 |
| DIV | 0x16 | ❌ | 除法 |
| BITAND | 0x17 | ❌ | 位与 |
| BITOR | 0x18 | ❌ | 位或 |
| BITXOR | 0x19 | ❌ | 位异或 |
| BITNOT | 0x1A | ❌ | 位取反 |
| SR | 0x1B | ✅ | SR 触发器 (置位优先) |
| RS | 0x1C | ✅ | RS 触发器 (复位优先) |
| COUNTER | 0x1D | ✅ | IEC 计数器 |

共 **28 种原语**，其中 **12 种有状态** (需要 state slot)。

---

## 6. 与 IEC 61131-3 的对应关系

| IEC 61131-3 标准 FB | DCL 关键字 | 原语 | 硬件验证 |
|---------------------|-----------|------|---------|
| TON (延时接通) | TIMER mode=TON | OP_TIMER | ✅ |
| TOF (延时断开) | TIMER mode=TOF | OP_TIMER | ✅ |
| TP (脉冲定时) | TIMER mode=TP | OP_TIMER | ✅ |
| CTU (加计数) | COUNTER CU= | OP_COUNTER | ✅ |
| CTD (减计数) | COUNTER CD= | OP_COUNTER | ✅ |
| CTUD (加减计数) | COUNTER CU=,CD= | OP_COUNTER | ✅ |
| SR (置位优先锁存) | LATCH | OP_SR | ✅ |
| RS (复位优先锁存) | — | OP_RS | ✅ |
| MUX (多路选择) | — | OP_MUX | ✅ |
| CLAMP (限幅) | PID LIMIT | OP_CLAMP | ✅ |
| SCALE (标定) | SCALE | OP_SCALE | ✅ |
| CMP (比较) | ALARM | OP_CMP | ✅ |
| FILTER (低通) | FILTER | OP_LPF | ✅ |

---

## 7. DCL 的局限性与后续扩展方向

### 7.1 当前局限

| 局限 | 说明 | 影响 |
|------|------|------|
| **无控制流** | 没有 IF/ELSE/FOR/WHILE | 复杂逻辑需要多个 ALARM+LOGIC 组合 |
| **无自定义 FB** | 不能封装可复用的功能块 | 类似程序必须复制粘贴 |
| **无表达式** | 不能写 `a + b * c` | 算术运算需要独立的 ADD/MUL 行 |
| **无数组** | 不支持数组操作 | 批量处理受限 |
| **无赋值** | 没有 `:=` 操作 | 不能直接设置常量 |
| **无枚举/类型** | 只有 float32 | 类型安全为零 |
| **LOGIC 只支持二元** | 只能 A AND B，不能 A AND B AND C | 链式逻辑需要多行 |

### 7.2 后续扩展候选

| 扩展 | 语法示例 | 编译到 |
|------|---------|--------|
| 常量定义 | `CONST pi = 3.14159` | SRC_CONST + param |
| 算术表达式 | `ARITH flow = dp * k` | OP_MUL |
| 加法 | `ARITH sum = a + b` | OP_ADD |
| 自定义 FB | `FB MyFilter(inp, alpha) → out { ... }` | 内联展开 |
| 多输入逻辑 | `LOGIC ok = a AND b AND c` | 链式 OP_AND |
| 条件选择 | `MUX output = sel ? a : b` | OP_MUX |
| 看门狗 | `WATCHDOG timeout=5s → Q=ok` | OP_TIMER |

---

## 8. 完整编程示例

### 8.1 温度 PID 控制（5 行）

```
# 温度 PID 控制系统
SENSOR temp FROM ADC1_CH0
FILTER temp_f FROM temp LOWPASS a=0.1
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
ALARM overheat FROM temp_f > 90
OUTPUT heat_pwm TO TIM1_CH1 FROM heater
```

编译结果: 6 routes, 4 params, 2 states, 6 wires, 24592 bytes

### 8.2 带安全连锁的控制（9 行）

```
# 带安全连锁的温度控制
SENSOR temp FROM ADC1_CH0
SENSOR btn FROM GPIO_PE5
FILTER temp_f FROM temp LOWPASS a=0.1
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
ALARM overheat FROM temp_f > 90
LATCH run: S1=btn, R=overheat → Q1=motor_on
LOGIC safe_heat = motor_on AND NOT overheat
OUTPUT heat_pwm TO TIM1_CH1 FROM heater
OUTPUT fault_led TO GPIO_PE6 FROM overheat
```

### 8.3 批次计数（6 行）

```
# 批次计数 + 定时关停
SENSOR part FROM GPIO_PE3
SENSOR start_btn FROM GPIO_PE4
COUNTER batch: CU=part, PV=100 → Q=batch_done, CV=count
TIMER shutdown: IN=batch_done, PT=2s → Q=stopped
LATCH running: S1=start_btn, R=stopped → Q1=active
OUTPUT motor TO TIM1_CH1 FROM active
```

---

## 9. DCL 与其他语言的定位关系

```
工业控制语言谱系:

  低级 ←————————————————————————→ 高级

  汇编    DCL     ST      LD/FBD    Python/C++
   │       │       │        │          │
   │       │       │        │          └ 通用编程
   │       │       │        └ 图形化 IEC 标准
   │       │       └ 文本式 IEC 标准
   │       └ 声明式数据流 (本项目)
   └ MCU 原生指令

DCL 的独特位置:
  - 比 ST 更简洁 (声明式 vs 命令式)
  - 比汇编更抽象 (FB 级别 vs 指令级别)
  - 比 LD/FBD 更精确 (文本 → 无歧义)
  - 编译到路由表 → 确定性执行 (这是核心竞争力)
```

**DCL 是后续 LD/FBD 的文本基础:**
- LD 的每个触点/线圈 → DCL 的 LOGIC/OUTPUT
- FBD 的每个方块+连线 → DCL 的每行 FB + FROM
- ST 的每个赋值 → DCL 未来的 ARITH/CONST

图形语言 (LD/FBD) 可以**自动生成** DCL 文本，然后复用同一套编译器。

---

*文档结束 — DCL 语言规范 v1.0*
