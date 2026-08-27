# DCL 系统闭环自查报告

> 审查范围: 原语层 → 编译器层 → 固件层 → 通信层 → IDE层  
> 审查日期: 2026-07-11  
> 审查结论: **发现 7 个致命问题 + 5 个严重问题 + 3 个一般问题**

---

## 一、原语层闭环自查

### 1.1 28 个原语逐个审查

| # | 原语 | 固件实现 | 编译器语法 | 第二输入方式 | 状态 | 闭环？ |
|---|------|---------|-----------|------------|------|--------|
| 0 | DIRECT | ✅ `return src` | ✅ SENSOR/OUTPUT | 无 | 无 | ✅ |
| 1 | CMP | ✅ `src > threshold` | ✅ ALARM | param.value_a(阈值) | 无 | ⚠️ 只支持>，不支持<,>=,<= |
| 2 | HYST | ✅ 迟滞比较 | ❌ **无语法** | param.value_a(高), value_b(低) | state_a | ❌ |
| 3 | CLAMP | ✅ 限幅 | ✅ PID LIMIT | param.value_a(lo), value_b(hi) | 无 | ✅ |
| 4 | LPF | ✅ 低通滤波 | ✅ FILTER | param.value_a(alpha) | state_a | ✅ |
| 5 | PID | ✅ PID控制 | ✅ PID | param(kp,ki,kd,sp) | state_a(积分),state_b(上次误差) | ⚠️ SP只能常量 |
| 6 | RATE | ✅ 变化率 | ✅ RATE | 无 | state_a(上次值) | ✅ |
| 7 | DEADBAND | ✅ 死区 | ✅ DEADBAND | param.value_a(宽度) | state_a | ✅ |
| 8 | MUX | ✅ 二选一 | ❌ **无语法** | param.value_a(假时WIRE), value_b(真时WIRE) | 无 | ❌ |
| 9 | EDGE | ✅ 边沿检测 | ❌ **无语法** | param.value_a(0=上升,1=下降,2=两者) | state_a(上次值) | ❌ |
| A | LUT | ✅ 查找表 | ❌ **无语法** | LUT_DATA[] | 无 | ❌ |
| B | CNT | ✅ 底层计数 | ❌ **无语法** (被COUNTER替代) | param.value_a(阈值) | state_a(计数),state_b(上次值) | N/A |
| C | TIMER | ✅ TON/TOF/TP | ✅ TIMER | param(mode,pt,is_et) | state(FSM,上次输入,ET) | ✅ |
| E | SCALE | ✅ y=kx+b | ✅ SCALE | param.value_a(k), value_b(b) | 无 | ✅ |
| F | AND | ✅ 逻辑与 | ✅ LOGIC | **param.value_a = 第二WIRE索引** | 无 | ✅ |
| 10 | OR | ✅ 逻辑或 | ✅ LOGIC | param.value_a = 第二WIRE索引 | 无 | ✅ |
| 11 | NOT | ✅ 逻辑非 | ✅ LOGIC | 无(只需一个输入) | 无 | ⚠️ 语法要求两个输入 |
| 12 | REG | ❌ **空实现!** | ❌ 无语法 | — | — | ❌ **致命** |
| 13 | ADD | ✅ 加法 | ❌ **无语法** | param.value_a = 第二WIRE索引 | 无 | ❌ |
| 14 | SUB | ✅ 减法 | ❌ **无语法** | param.value_a = 第二WIRE索引 | 无 | ❌ |
| 15 | MUL | ✅ 乘法 | ❌ **无语法** | param.value_a = 第二WIRE索引 | 无 | ❌ |
| 16 | DIV | ✅ 除法(除零保护) | ❌ **无语法** | param.value_a = 第二WIRE索引 | 无 | ❌ |
| 17 | BITAND | ⚠️ **固件无case** | ❌ 无语法 | — | — | ❌ **致命** |
| 18 | BITOR | ⚠️ **固件无case** | ❌ 无语法 | — | — | ❌ **致命** |
| 19 | BITXOR | ⚠️ **固件无case** | ❌ 无语法 | — | — | ❌ **致命** |
| 1A | BITNOT | ⚠️ **固件无case** | ❌ 无语法 | — | — | ❌ **致命** |
| 1B | SR | ✅ 置位优先 | ✅ LATCH | param.value_a = R的WIRE索引 | state_a | ✅ |
| 1C | RS | ✅ 复位优先 | ❌ **无语法** | param.value_a = R1的WIRE索引 | state_a | ❌ |
| 1D | COUNTER | ✅ CTU/CTD/CTUD | ✅ COUNTER | param(mode,PV,out_sel,aux_wire) | state(CV,上次值) | ✅ |

### 1.2 原语层闭环统计

```
固件已实现:  23/28 (82%)  — BITAND/BITOR/BITXOR/BITNOT 无case, REG空实现
编译器可触达: 13/28 (46%) — 15个原语无DCL语法
完全闭环:    11/28 (39%) — 固件+编译器+语法 三位一体
```

---

## 二、致命问题（7个）

### 致命1: OP_REG 空实现

```c
case OP_REG:
    // 空的! 直接fallthrough到下面的代码
case OP_TIMER: {
```

**影响:** REG 是锁存/寄存器原语，但固件里没有实现体。`case OP_REG:` 直接 fallthrough 到 `OP_TIMER`，导致 REG 操作实际执行的是 TIMER 逻辑！

**修复:** 要么实现 REG，要么删除这个 case。

---

### 致命2: BITAND/BITOR/BITXOR/BITNOT 固件未实现

```c
// 枚举定义存在:
OP_BITAND = 0x17, OP_BITOR = 0x18, OP_BITXOR = 0x19, OP_BITNOT = 0x1A,
// 但 switch 里没有对应的 case!
// 会走到 default: return src; — 原样返回输入，静默失败
```

**影响:** 4 个位运算原语定义了操作码但没有实现，调用时静默返回输入值，不报错。

**修复:** 在 `execute_primitive()` 中添加 4 个 case。

---

### 致命3: CMP 只支持 `>` 运算符

```c
case OP_CMP:
    return (src > p->value_a) ? 1.0f : 0.0f;  // 只有 > !
```

**编译器生成的参数:**
```python
cmp_mode = {'>': 0, '>=': 1, '<': 2, '<=': 3}[op]
pi = self.alloc_param(th, float(cmp_mode), 0, 0)
```

编译器正确计算了 `cmp_mode`（0/1/2/3）并存入 `param.value_b`，但固件**完全不读取 `value_b`**！只做 `>` 比较。`>=`, `<`, `<=` 三种比较模式在编译器里写了但固件不执行。

**影响:** `ALARM x FROM sig < 80` 编译通过但运行时行为错误（实际做的是 `> 80`）。

**修复:** 固件 CMP 实现:
```c
case OP_CMP: {
    int mode = (int)p->value_b;
    if (mode == 0) return (src > p->value_a) ? 1.0f : 0.0f;
    if (mode == 1) return (src >= p->value_a) ? 1.0f : 0.0f;
    if (mode == 2) return (src < p->value_a) ? 1.0f : 0.0f;
    if (mode == 3) return (src <= p->value_a) ? 1.0f : 0.0f;
    return 0.0f;
}
```

---

### 致命4: OUTPUT 不产生任何物理输出

```c
// ISR 循环末尾:
WIRE_MAP[rp[3]] = out;  // 所有路由都只是写到 WIRE

// PWM 占空比更新是硬编码的:
*(volatile uint16_t *)(TIM1_BASE + 0x34) = 6800;  // 固定值!
*(volatile uint16_t *)(TIM1_BASE + 0x38) = 3400;  // 固定值!
*(volatile uint16_t *)(TIM1_BASE + 0x3C) = 10200; // 固定值!

// 数字输出也是硬编码的:
SHADOW_GPIO = HEARTBEAT & 1;  // 只是心跳翻转!
```

**影响:** `OUTPUT heat_pwm TO TIM1_CH1 FROM heater` — 编译器生成了路由，WIRE 值在更新，但**没有任何代码把 WIRE 值写到 PWM 寄存器或 GPIO**。`actuator_idx` 字段永远是 0，ISR 不使用它。

**这等于说：整个控制系统的输出是断路的！** PID 算出了完美的控制量，但它永远到不了执行器。

**修复:** ISR 循环后需要增加输出映射:
```c
// 输出映射: WIRE → 物理执行器
for (int i = 0; i < n; i++) {
    RouteEntry_t *r = &ROUTE_TABLE[i];
    if (r->actuator_idx > 0) {
        uint16_t ai = r->actuator_idx;
        float val = WIRE_MAP[r->dst_channel];
        ACTUATOR_STATUS[ai] = val;
        // 根据 actuator_idx 类型写入对应硬件
        if (ai == 1) TIM1_CCR1 = (uint16_t)(val / 100.0f * 13599);
        else if (ai == 2) TIM1_CCR2 = (uint16_t)(val / 100.0f * 13599);
        else if (ai >= 32) SHADOW_GPIO |= (1 << (ai - 32));
    }
}
```

---

### 致命5: SENSOR_MAP 映射不完整

```c
// ISR 读取:
src = SENSOR_MAP[rp[1]];  // rp[1] = src_index

// 但编译器生成的:
'src_type': SRC_SENSOR, 'src_index': 0,  // 永远是0！
```

编译器把所有 SENSOR 的 `src_index` 设为 0，意味着**所有 SENSOR 都读 SENSOR_MAP[0]**，即同一个 ADC 通道。多个 SENSOR 时，它们读到的是同一个值。

编译器有 `self.sensors = {name: source}` 字典记录了哪个信号对应哪个硬件源（如 "ADC1_CH0"），但**没有把这个映射编码进路由表二进制**。

**影响:** `SENSOR temp FROM ADC1_CH0` 和 `SENSOR pressure FROM ADC1_CH1` — 两者都读 SENSOR_MAP[0]。

**修复:** 编译器需要维护传感器源→索引的映射，并在二进制输出中包含 SENSOR_MAP 配置。

---

### 致命6: wire2_idx 从未被固件使用

```c
uint16_t wire2_idx;  /* CTUD: R/LD wire index */
```

注释说 wire2_idx 用于 CTUD，但固件 ISR 主循环**完全不读这个字段**。CTUD 的 CD 信号是通过 `p->value_d`（param.value_d）传递的，不是 wire2_idx。

编译器也从不设置 wire2_idx（永远是 0）。

**影响:** 这是一个浪费的 16 位字段，目前无任何功能。如果未来有人尝试使用它来传递第二输入，会发现它被忽略。

---

### 致命7: PID 积分限幅硬编码 ±100

```c
case OP_PID: {
    float acc = s->state_a + p->value_b * err;
    acc = (acc >  100.0f) ?  100.0f : acc;  // 硬编码!
    acc = (acc < -100.0f) ? -100.0f : acc;  // 硬编码!
```

积分限幅 ±100 是硬编码的，无法通过参数配置。对于输出范围 0~100 的 PID 没问题，但对于输出范围 0~1000 或 -32768~32767 的场景会严重限制性能。

**修复:** 用 `param.value_c` 的某个保留位作为积分限幅，或在 CLAMP 后将限幅值回写。

---

## 三、严重问题（5个）

### 严重1: LOGIC NOT 语法要求两个输入

```
LOGIC name = signal1 NOT signal2
```

固件 `OP_NOT` 只需要一个输入: `return (src > 0.5f) ? 0.0f : 1.0f;`

但 DCL 语法强制写两个信号名，`signal2` 被编译器存入 param.value_a 但固件不使用。语法设计不自然。

**应该是:** `LOGIC name = NOT signal`

---

### 严重2: PID 设定值 (SP) 不可动态改变

```c
float err = p->value_d - src;  /* sp - src */
```

SP 写在 param 表里，是编译期常量。运行时无法通过 WIRE 动态调整 SP。

实际需求:
- 串级控制: 外环 PID 输出作为内环 SP
- 配方切换: 不同产品不同 SP
- 远程设定: HMI 通过通信修改 SP

**修复方案:** 增加 `SP_FROM wire` 语法，编译器生成路由时从 WIRE 读取 SP。

---

### 严重3: 编译器不输出 SENSOR_MAP 和 ACTUATOR_MAP

编译器 `generate_binary()` 只输出:
- 路由表 + 参数表 + CRC32

缺少:
- SENSOR_MAP 配置 (哪个传感器读哪个 ADC 通道)
- ACTUATOR_MAP 配置 (哪个输出对应哪个 PWM/GPIO)
- LUT_DATA 配置 (查找表数据)

DEPLOY 时 H723 无法知道如何映射物理 IO。

---

### 严重4: 固件测试程序硬编码 PWM 值

```c
*(volatile uint16_t *)(TIM1_BASE + 0x34) = 6800;  // 硬编码 50%
*(volatile uint16_t *)(TIM1_BASE + 0x38) = 3400;  // 硬编码 25%
*(volatile uint16_t *)(TIM1_BASE + 0x3C) = 10200; // 硬编码 75%
```

这是 v1.9 的验证测试代码，PWM 占空比是固定的，不从 WIRE 读取。正式产品需要从 WIRE_MAP 映射到 CCR。

---

### 严重5: CTUD 模式中 R 信号硬编码读取 WIRE[200]

```c
float r = WIRE_MAP[(int)200];  // 硬编码!
```

CTUD 模式的复位信号读取的是固定 WIRE[200]，而不是用户指定的信号。这应该是编译器通过参数传递的，但当前实现是硬编码。

---

## 四、一般问题（3个）

### 一般1: SRC_CONST 实现方式有歧义

```c
else // SRC_CONST
    src = PARAM_TABLE[*(uint16_t *)(rp+6)].value_d;
```

SRC_CONST 时，输入值从 param.value_d 读取。这意味着一个路由如果是 SRC_CONST，它的 param 同时服务于两个目的：value_d 是输入值，value_a/b/c 可能是操作参数。这可能产生冲突。

---

### 一般2: ISR 内联优化只覆盖 3 个原语

```c
if (op == OP_DIRECT)
    out = src;
else if (op == OP_SCALE)
    out = ...;
else if (op == OP_CMP)
    out = ...;
else
    out = execute_primitive(op, src, ...);  // 函数调用
```

只有 DIRECT/SCALE/CMP 内联，其余 25 个走函数调用。高频路径（PID/LPF/CLAMP）也应该内联。

---

### 一般3: DEADBAND 语义与预期不符

```c
case OP_DEADBAND: {
    float diff = src - s->state_a;  // 与上次输出的差
    if (diff < -p->value_a || diff > p->value_a) {
        s->state_a = src;
        return src;
    }
    return s->state_a;
}
```

这是**滞回死区**（与上次输出比较），不是传统死区（与零点比较）。语义与文档描述不一致。

---

## 五、闭环完整度评分

```
层级              闭环度    说明
────────────────────────────────────────────────
原语层 (固件)      75%     REG空,4个BIT无case,CMP不完整
编译器层 (DCL)     46%     15/28原语无语法
传感器映射          0%     src_index永远=0,物理通道映射缺失
执行器映射          0%     actuator_idx未使用,PWM硬编码
通信协议 (USB)      10%    帧格式定义了,固件端未实现
IDE层             60%     编辑器+编译OK,监控+部署未端到端验证
────────────────────────────────────────────────
系统整体闭环度      ~25%
```

---

## 六、修复优先级

| 优先级 | 问题 | 修复量 | 阻塞程度 |
|--------|------|--------|---------|
| **P0** | OUTPUT不产生物理输出 | 固件20行 | 整个系统无输出 |
| **P0** | SENSOR src_index永远0 | 编译器+固件30行 | 多传感器不可用 |
| **P0** | CMP不支持<,>=,<= | 固件5行 | ALARM功能半残 |
| **P1** | REG空实现+fallthrough | 固件5行 | 静默错误 |
| **P1** | 4个BIT原语无实现 | 固件16行 | 位运算不可用 |
| **P1** | CTUD的R信号硬编码WIRE[200] | 固件2行 | CTUD不可用 |
| **P1** | DCL缺少15个原语的语法 | 编译器100行 | 53%硬件能力闲置 |
| **P2** | PID积分限幅硬编码 | 固件3行 | 特定场景受限 |
| **P2** | PID SP不可动态 | 编译器+固件20行 | 串级控制不可用 |
| **P2** | 编译器不输出IO映射 | 编译器50行 | DEPLOY不完整 |

---

*自查报告结束 — 发现 7 个致命问题，需要立即修复才能进入正式开发*
