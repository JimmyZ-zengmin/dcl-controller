# DCL 原语参考（28 种）

> 全部功能块（FB）按类别组织。每个原语标注语法、参数、算法、示例与验证状态。  
> 验证状态来自 v2.0 编程规范完整测试。

---

## 1. 输入输出类

### SENSOR — 硬件输入

```
SENSOR <name> FROM <source> [SCALE <k> <b>] [RANGE <lo> <hi>]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 信号名，分配到 WIRE |
| `source` | ✅ | 硬件源: `ADC1_CH0`, `GPIO_PE5` 等 |
| `SCALE k b` | ❌ | 线性标定: `y = k × x + b` |
| `RANGE lo hi` | ❌ | 等价于 `SCALE hi-lo lo` |

**算法:** `value = raw × k + b`。省略 SCALE/RANGE 时 `k=1.0, b=0.0`。

```
SENSOR temp FROM ADC1_CH0
SENSOR voltage FROM ADC1_CH1 SCALE 0.001 0.0
SENSOR pressure FROM ADC1_CH2 RANGE 0 3.3    # 等价 SCALE 3.3 0
```

✅ 已验证 | 生成路由: 1

---

### OUTPUT — 硬件输出

```
OUTPUT <name> TO <target> [FROM <signal>]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 输出实例名 |
| `target` | ✅ | 硬件目标: `TIM1_CH1`, `GPIO_PE5` 等 |
| `FROM signal` | ❌ | 输入信号名，省略时 `signal = name` |

```
OUTPUT heat_pwm TO TIM1_CH1 FROM heater
OUTPUT fault_led TO GPIO_PE5 FROM fault
OUTPUT heat TO TIM1_CH1                       # 等价 FROM heat
```

✅ 已验证 | 生成路由: 1

---

### CONST — 常量声明

```
CONST <name> = <value>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 常量名称 |
| `value` | ✅ | 浮点数值 |

```
CONST setpoint = 60.0
CONST kp = 2.0
```

✅ 已验证 | 生成路由: 0（编译期展开）

---

## 2. 数学运算类

### ARITH — 四则运算

```
ARITH <name> = <src1> <op> <src2>
```

| 操作符 | 说明 |
|--------|------|
| `ADD` | 加法 |
| `SUB` | 减法 |
| `MUL` | 乘法 |
| `DIV` | 除法 |

`src1` 和 `src2` 必须是已声明的信号名或常量。

```
ARITH sum = a ADD b
ARITH diff = a SUB b
ARITH prod = a MUL b
ARITH quot = a DIV b
```

✅ 已验证 | 生成路由: 1

---

### LIMIT — 限幅

```
LIMIT <name> FROM <src> RANGE <lo> <hi>
```

```
LIMIT clamped FROM temp RANGE -10 10
LIMIT percent FROM temp RANGE 0 100
```

✅ 已验证 | 生成路由: 1

---

### MAX — 取大

```
MAX <name> = <src> MAX <value>
```

```
MAX max_val FROM temp MAX 5.0
```

✅ 已验证 | 生成路由: 1

---

### MIN — 取小

```
MIN <name> = <src> MIN <value>
```

```
MIN min_val FROM temp MIN 3.0
```

✅ 已验证 | 生成路由: 1

---

### ABS — 绝对值

```
ABS <name> FROM <src>
```

```
ABS abs_val FROM temp
```

✅ 已验证 | 生成路由: 1

---

### SCALE — 线性标定

```
SCALE <name> FROM <src> RANGE <lo> <hi>
```

**算法:** `y = (hi - lo) × x + lo`

与 SENSOR 的 SCALE/RANGE 子句的区别：SENSOR 的 SCALE 在读取时做标定，独立 SCALE 可以在管线中间任意位置做映射。

```
SCALE percent FROM raw RANGE 0 100
SCALE celsius FROM adc_val RANGE 0 150
```

✅ 已验证 | 生成路由: 1

---

### RATE — 变化率限制

```
RATE <name> FROM <src> MAX <rate>
```

**算法:** `y = clamp(x_current - x_previous, -rate, rate)` — 限制信号每周期最大变化量。

**状态:** 有状态（保存上一周期值）

```
RATE temp_rated FROM temp MAX 10.0           # 每周期变化不超过 10.0
```

✅ 已验证 | 生成路由: 1

---

### DEADBAND — 死区滤波

```
DEADBAND <name> FROM <src> [, <width>]
```

**算法:** `|x| < width → y = 0`，否则 `y = x`。参数可选，默认 width=0。

**状态:** 有状态

```
DEADBAND clean FROM noisy, 0.5               # 消除 ±0.5 以内的小波动
DEADBAND dead FROM signal                     # width=0，等效 DIRECT
```

✅ 已验证 | 生成路由: 1

---

## 3. 比较运算类

### EQ — 等于比较

```
EQ <name> FROM <src> == <value>
```

**输出:** `1.0`（相等）或 `0.0`（不等）

```
EQ is_zero FROM temp == 0.0
EQ is_five FROM temp == 5.0
```

✅ 已验证 | 生成路由: 1

---

### NE — 不等于比较

```
NE <name> FROM <src> != <value>
```

**输出:** `1.0`（不等）或 `0.0`（相等）

```
NE not_zero FROM temp != 0.0
```

✅ 已验证 | 生成路由: 1

---

### HYST — 滞回比较器

```
HYST <name> FROM <src> HIGH <hi> LOW <lo>
```

防止在阈值附近抖动的滞回比较：
- 当 `src > hi` 时，输出 `1.0`
- 当 `src < lo` 时，输出 `0.0`
- 在 `lo` 和 `hi` 之间时，保持之前的状态

**状态:** 有状态

```
HYST heater_on FROM temp HIGH 80 LOW 75
```

✅ 已验证 | 生成路由: 1

---

### ALARM — 报警触发

```
ALARM <name> FROM <src> <op> <threshold>
```

| 操作符 | 说明 |
|--------|------|
| `>` | 大于 |
| `>=` | 大于等于 |
| `<` | 小于 |
| `<=` | 小于等于 |

**输出:** `1.0`（触发）或 `0.0`（正常）

```
ALARM overheat FROM temp_f > 90
ALARM low_press FROM pressure <= 0.5
```

✅ 已验证 | 生成路由: 1

---

## 4. 逻辑与位运算类

### LOGIC — 逻辑运算

```
LOGIC <name> = <src1> <op> <src2>
LOGIC <name> = NOT <src>
```

| 操作符 | 说明 |
|--------|------|
| `AND` | 逻辑与（两个输入均 >0.5 则输出 1.0） |
| `OR` | 逻辑或（任一输入 >0.5 则输出 1.0） |
| `NOT` | 逻辑非（单操作数） |

```
LOGIC system_ready = temp_ok AND pressure_ok
LOGIC fault = NOT system_ready
LOGIC alarm = overtemp OR overpressure
```

✅ 已验证 | 生成路由: 1

---

### BIT — 位运算

```
BIT <name> = <src1> <op> <src2>
BIT <name> = BITNOT <src>
```

| 操作符 | 说明 |
|--------|------|
| `BITAND` | 位与 |
| `BITOR` | 位或 |
| `BITXOR` | 位异或 |
| `BITNOT` | 位取反（单操作数） |

```
BIT masked = flags BITAND mask
BIT inverted = BITNOT flags
BIT combined = flags BITOR mask
```

✅ 已验证 | 生成路由: 1

---

## 5. 时序控制类

### TIMER — 定时器

```
TIMER <name>: IN=<signal>, PT=<time> → Q=<output>
                                           [, ET=<elapsed>]
                                           [, mode=<TON|TOF|TP>]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `IN` | ✅ | 输入信号（触发条件） |
| `PT` | ✅ | 预设时间，带单位 `s` 或 `ms` |
| `mode` | ❌ | `TON`（默认，延时接通）、`TOF`（延时断开）、`TP`（脉冲） |
| `Q` | ✅ | 输出信号 |
| `ET` | ❌ | 经过时间信号 |

**行为对照（IEC 61131-3）:**
- **TON**: IN=1 开始计时，到达 PT 后 Q=1
- **TOF**: IN=1 → Q=1；IN=0 开始计时，到达 PT 后 Q=0
- **TP**: IN 上升沿触发固定宽度脉冲

**状态:** 有状态

```
TIMER t1: IN=btn, PT=3s → Q=motor_on                   # TON 3秒
TIMER delay: IN=start, PT=500ms, mode=TOF → Q=hold     # TOF 500ms
TIMER pulse: IN=trigger, PT=100ms, mode=TP → Q=flash   # TP 100ms脉冲
TIMER t2: IN=switch, PT=2s → Q=ready, ET=elapsed       # 带经过时间
```

✅ 已验证 | 生成路由: 1（无 ET）或 2（有 ET）

---

### COUNTER — 计数器

支持三种 IEC 61131-3 计数器模式：

```
# CTU（加计数，默认）
COUNTER <name>: CU=<signal>, PV=<preset> → Q=<output>, CV=<count>

# CTD（减计数）
COUNTER <name>: CD=<signal>, PV=<preset> → Q=<output>, CV=<count>

# CTUD（加减计数）
COUNTER <name>: CU=<signal>, CD=<signal> [, PV=<preset>] → Q=<output>, CV=<count>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `CU` | ✅（CTU/CTUD） | 加计数输入（上升沿 +1） |
| `CD` | ✅（CTD/CTUD） | 减计数输入（上升沿 -1） |
| `PV` | ❌ | 预设值（默认 100） |
| `Q` | ✅ | 到达预设值时输出 `1.0` |
| `CV` | ✅ | 当前计数值 |

**IEC 61131-3 对应:** CTU（加计数），CTD（减计数），CTUD（加减计数）

**状态:** 有状态

```
COUNTER parts: CU=part_detected, PV=100 → Q=batch_done, CV=parts_count
COUNTER countdown: CD=tick, PV=60 → Q=timeout, CV=remaining
COUNTER bidir: CU=up_cmd, CD=down_cmd, PV=50 → Q=at_limit, CV=pos
```

✅ 已验证 | 生成路由: 2

---

### EDGE — 边沿检测

```
EDGE <name> FROM <src> <direction>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `direction` | ✅ | `RISING`（上升沿）或 `FALLING`（下降沿） |

检测到边沿时输出 `1.0`，否则输出 `0.0`。

**状态:** 有状态

```
EDGE btn_pressed FROM btn RISING
EDGE btn_released FROM btn FALLING
```

✅ 已验证 | 生成路由: 1

---

## 6. 信号处理与触发器类

### FILTER — 低通滤波

```
FILTER <name> FROM <src> LOWPASS a=<alpha>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `alpha` | ✅ | 滤波系数 0~1，越小滤波越强 |

**算法:** `y += alpha × (x - y)` — 一阶 IIR 低通滤波

**状态:** 有状态（保存上一次输出 y）

```
FILTER temp_f FROM temp LOWPASS a=0.1       # 强滤波
FILTER fast_f FROM sensor LOWPASS a=0.5     # 轻滤波
```

✅ 已验证 | 生成路由: 1

---

### PID — PID 控制器

```
PID <name> FROM <src> SP=<sp> KP=<kp> KI=<ki> KD=<kd> [LIMIT <lo> <hi>]
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `SP` | ✅ | 设定值 |
| `KP` | ✅ | 比例增益 |
| `KI` | ✅ | 积分增益 |
| `KD` | ✅ | 微分增益 |
| `LIMIT lo hi` | ❌ | 输出限幅（默认 `0 100`） |

**编译行为:** 生成 2 条路由: `PID` 计算 + `CLAMP` 限幅。自动创建中间 WIRE `{name}_pid`（PID 原始输出）和 `{name}`（限幅后输出）。

**状态:** 有状态（保存积分项和上一次误差）

```
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
PID pressure_ctrl FROM pressure SP=50 KP=1.5 KI=0.05 KD=0.01 LIMIT 0 200
```

✅ 已验证 | 生成路由: 2 | 生成参数: 2（PID 参数 + CLAMP 参数）

---

### LATCH — SR 触发器（置位优先）

```
LATCH <name>: S1=<set>, R=<reset> → Q1=<output>
```

| 逻辑 | Q1 |
|------|----|
| S1=1, R=0 | 1 |
| S1=0, R=1 | 0 |
| S1=0, R=0 | 保持 |
| S1=1, R=1 | **1**（置位优先） |

```
LATCH motor_run: S1=start_btn, R=stop_btn → Q1=running
```

✅ 已验证 | 生成路由: 1

---

### RLATCH — RS 触发器（复位优先）

```
RLATCH <name>: S=<set>, R1=<reset> → Q1=<output>
```

| 逻辑 | Q1 |
|------|----|
| S=1, R1=0 | 1 |
| S=0, R1=1 | 0 |
| S=0, R1=0 | 保持 |
| S=1, R1=1 | **0**（复位优先） |

```
RLATCH safe: S=start, R1=estop → Q1=active
```

✅ 已验证 | 生成路由: 1

---

### MUX — 多路选择器

```
MUX <name> = <value_true> SELECT <condition> ELSE <value_false>
```

当 `condition > 0.5` 时输出 `value_true`，否则输出 `value_false`。

```
MUX output = manual_val SELECT is_manual ELSE auto_val
```

✅ 已验证 | 生成路由: 1

---

### LUT — 一维查表

```
LUT <name> FROM <src> TABLE <v1> <v2> <v3> ...
```

一维线性插值查表。输入值在表索引之间线性插值。

```
LUT curve FROM temp TABLE 0.0 0.5 1.0 0.8 0.3
```

✅ 已验证 | 生成路由: 1
