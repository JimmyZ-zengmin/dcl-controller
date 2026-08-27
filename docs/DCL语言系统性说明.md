# DCL语言系统性说明

> 版本: v1.0  
> 日期: 2026-07-12

---

## 一、DCL是什么？

**DCL（Deterministic Control Language）** 是一种专为PLC（可编程逻辑控制器）编程设计的**领域特定语言（DSL）**。

### 核心定位

```
通用编程语言（C/Python）    → 图灵完备，什么都能做
PLC梯形图（Ladder Logic）   → 电气工程师友好，但表达能力有限
  ↓
DCL                         → 文本化PLC编程语言，兼顾表达能力和工程友好
```

### 本质

DCL是**IEC 61131-3工业标准**的文本化实现。IEC 61131-3定义了5种PLC编程语言：
- LD（梯形图 Ladder Diagram）
- FBD（功能块图 Function Block Diagram）
- SFC（顺序功能图 Sequential Function Chart）
- ST（结构化文本 Structured Text）
- IL（指令表 Instruction List）

DCL类似于**简化版的ST（结构化文本）**，但做了以下改进：
1. **语句更简洁** — 每条语句对应一个硬件原语
2. **关键词更直观** — `SENSOR`、`LIMIT`、`PID`、`OUTPUT`
3. **直接映射硬件** — 编译后直接生成STM32H723可执行的二进制

---

## 二、DCL与硬件的关系

### 执行模型

```
┌──────────────────────────────────────────────────────────────┐
│                         DCL 源文件                           │
│   SENSOR temp FROM ADC1_CH0                                  │
│   PID heater FROM temp SP=60 KP=2.0 KI=0.1 LIMIT 0 100       │
│   OUTPUT heater TO TIM1_CH1                                  │
└──────────────────────────┬───────────────────────────────────┘
                           │ 编译（DCL Compiler）
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                     二进制路由表                              │
│   [Route 0] src=SENSOR_ADC1_CH0, op=PID, params=[60,2,0.1,0]│
│   [Route 1] src=wire[0], op=OUTPUT, dst=TIM1_CH1            │
└──────────────────────────┬───────────────────────────────────┘
                           │ 写入（pyocd）
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                  STM32H723 固件 (100μs ISR)                  │
│   每个周期:                                                   │
│   1. 读取 ADC1_CH0 → WIRE[0]                                │
│   2. 执行 PID(WIRE[0], SP=60, KP=2.0, KI=0.1) → WIRE[1]    │
│   3. 输出 WIRE[1] → TIM1_CH1 (PWM)                          │
└──────────────────────────────────────────────────────────────┘
```

### 关键概念

| 概念 | 说明 |
|------|------|
| **Route（路由）** | 一个基本运算单元，对应一次传感器读取/数学运算/PID计算等 |
| **Wire（导线）** | 运算结果的存储位置，每个Wire是一个浮点数 |
| **Param（参数）** | 路由的参数，如PID的KP/KI/KD/SP |
| **State（状态）** | 有状态运算的内部变量（如PID的积分器、滤波器的历史值） |
| **Sensor（传感器）** | 硬件输入源（ADC、GPIO等） |
| **Actuator（执行器）** | 硬件输出目标（PWM、GPIO等） |

### 硬件寄存器映射

```
0x20000000 ┌──────────────┐
           │ DTCM Base    │
0x20000100 ├──────────────┤
           │ Sensor Map   │  ← 硬件ADC值自动写入
0x20000200 ├──────────────┤
           │ Actuator Map │  ← OUTPUT从这里读取
0x20000300 ├──────────────┤
           │ Wire Map     │  ← 所有Wire值存储于此
0x20001700 ├──────────────┤
           │ Route Table  │  ← DCL编译器生成的路由表
0x20005700 ├──────────────┤
           │ Param Table  │  ← 各路由的参数
0x20007700 ├──────────────┤
           │ State Table  │  ← 有状态原语的内部状态
           └──────────────┘
```

---

## 三、DCL的28种原语（Primitives）

DCL编译器支持**28种基本操作（原语）**，分为以下几类：

### 3.1 输入输出类

| 原语 | 语法 | 说明 |
|------|------|------|
| SENSOR | `SENSOR name FROM source [SCALE k b]` | 读取传感器 |
| OUTPUT | `OUTPUT name TO port` | 输出到执行器 |
| CONST | `CONST name = value` | 常量声明 |

### 3.2 数学运算类

| 原语 | 语法 | 说明 |
|------|------|------|
| ARITH | `ARITH name = src OP src` | 四则运算（ADD/SUB/MUL/DIV） |
| LIMIT | `LIMIT name FROM src RANGE lo hi` | 限幅 |
| MAX | `MAX name = src MAX value` | 取大 |
| MIN | `MIN name = src MIN value` | 取小 |
| ABS | `ABS name FROM src` | 绝对值 |
| SCALE | 已合并到SENSOR | 缩放 |

### 3.3 比较运算类

| 原语 | 语法 | 说明 |
|------|------|------|
| EQ | `EQ name FROM src == value` | 等于 |
| NE | `NE name FROM src != value` | 不等于 |
| HYST | `HYST name FROM src HIGH hi LOW lo` | 滞回比较 |

### 3.4 逻辑运算类

| 原语 | 语法 | 说明 |
|------|------|------|
| LOGIC | `LOGIC a = b AND/OR/NOT c` | 逻辑运算 |
| BIT | `BIT a = b BITAND/BITOR/BITNOT c` | 位运算 |
| LATCH | `LATCH name: S1=s, R=r → Q1=q` | RS触发器 |
| RLATCH | `RLATCH name: S=s, R1=r1 → Q1=q` | 复位优先触发器 |

### 3.5 时序控制类

| 原语 | 语法 | 说明 |
|------|------|------|
| TIMER | `TIMER t1: IN=btn, PT=3s → Q=motor_on` | 定时器 |
| COUNTER | `COUNTER c1: CU=sensor, PV=100 → Q=full, CV=count` | 计数器 |
| EDGE | `EDGE name FROM src RISING/FALLING` | 边沿检测 |

### 3.6 信号处理类

| 原语 | 语法 | 说明 |
|------|------|------|
| FILTER | `FILTER name FROM src LOWPASS a=0.1` | 低通滤波 |
| PID | `PID name FROM src SP=val KP=x KI=y KD=z LIMIT lo hi` | PID控制 |
| RATE | 待验证 | 变化率限制 |
| DEADBAND | 待验证 | 死区 |
| MUX | `MUX name = src1 SELECT src2 ELSE src3` | 多路选择 |
| LUT | `LUT name FROM src TABLE v1 v2 v3...` | 查表 |

### 3.7 报警类

| 原语 | 语法 | 说明 |
|------|------|------|
| ALARM | `ALARM name FROM src > value` | 报警触发 |

---

## 四、DCL与其他PLC语言的对比

### 4.1 对比梯形图（LD）

```
梯形图:                              DCL:
  ┌──[>80]──( )──┐                   ALARM overheat FROM temp > 80
  │              │                   OUTPUT overheat TO GPIO_PE5
  └──[<75]──( )──┘
```

- **梯形图**: 图形化，电气工程师熟悉，但复杂逻辑画出来很乱
- **DCL**: 文本化，复杂逻辑清晰可读，版本控制友好

### 4.2 对比结构化文本（ST）

```pascal
// 标准ST（IEC 61131-3）
IF temp > 80 THEN
    overheat := TRUE;
ELSE
    overheat := FALSE;
END_IF;

// DCL（更简洁）
EQ overheat FROM temp == 80
```

- **ST**: 通用性强，但繁琐
- **DCL**: 每条语句直接映射硬件原语，编译后更高效

### 4.3 对比C语言

```c
// C代码（需要手动实现PID）
float error = setpoint - temp;
integral += error * dt;
float output = kp*error + ki*integral + kd*(error-last_error)/dt;

// DCL（硬件原生支持）
PID heater FROM temp SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
```

- **C**: 需要自己实现所有算法
- **DCL**: 每个原语是硬件固件原生支持的，100μs内完成计算

---

## 五、DCL的设计哲学

### 5.1 语句 = 原语

每条DCL语句直接对应固件中的一个**硬件原语**。这意味着：
- 无复杂控制流（无if/for/while）
- 无函数调用
- 每条语句在一个周期（100μs）内完成

这不是缺陷，而是**设计选择**：工业控制需要确定性的执行时间。

### 5.2 Wire = 信号

所有运算结果存储在**Wire**中。Wire就是一个浮点数组，索引由编译器自动分配。

```
temp → WIRE[0]
clamped → WIRE[1]
heater → WIRE[2]
```

### 5.3 数据流驱动

DCL是**数据流语言**，不是命令式语言：

```
# 数据流（DCL）：声明数据之间的关系
SENSOR temp FROM ADC1_CH0
PID heater FROM temp SP=60 KP=2.0
OUTPUT heater TO TIM1_CH1

# 命令式（C）：指定执行步骤
while(1) {
    temp = read_adc();
    heater = pid_calc(temp);
    write_pwm(heater);
}
```

---

## 六、DCL编译器的工作

### 编译流程

```
DCL源文件 → 解析（parse）→ 中间表示 → 代码生成 → 二进制路由表
```

### 二进制格式

```
┌─────────────────────────────────────────────────────────┐
│ Header (32 bytes)                                       │
│   magic: 'DCL2' (4 bytes)                               │
│   version, routes, params, states, wires (各4 bytes)     │
│   crc32 (4 bytes)                                       │
├─────────────────────────────────────────────────────────┤
│ Route Table (routes × 16 bytes)                         │
│   [Route 0] src_type, src_idx, dst_type, dst_ch, op...  │
│   [Route 1] ...                                         │
├─────────────────────────────────────────────────────────┤
│ Param Table (params × 16 bytes)                         │
│   [Param 0] value_a, value_b, value_c, value_d          │
├─────────────────────────────────────────────────────────┤
│ State Table (states × 16 bytes)                         │
│   [State 0] reserved for stateful ops                   │
└─────────────────────────────────────────────────────────┘
```

---

## 七、总结

| 维度 | DCL |
|------|-----|
| 语言类型 | 领域特定语言（DSL） |
| 范式 | 数据流/声明式 |
| 执行目标 | STM32H723 固件 |
| 执行模型 | 路由表驱动，100μs ISR |
| 核心概念 | Route / Wire / Param / State |
| 原语数量 | 28种 |
| 适用场景 | 工业控制、PLC、嵌入式实时控制 |
| 对标 | IEC 61131-3 ST语言的简化版 |
| 优势 | 简洁、确定性执行、硬件原生支持 |

---

**文档状态**: 正式版  
**适用范围**: DCL IDE v2.0 及以上版本
