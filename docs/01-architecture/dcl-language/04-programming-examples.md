# 编程规范与示例

---

## 1. 编程规范

### 1.1 命名规范

- **信号名**: 小写字母和下划线，如 `temp`, `pressure_clamped`, `is_high`
- **常量名**: 小写或驼峰，如 `setpoint`, `maxTemp`
- **硬件源**: 全大写加下划线，如 `ADC1_CH0`, `TIM1_CH1`, `GPIO_PE5`
- **FB 关键字**: 全大写，如 `SENSOR`, `PID`, `FILTER`
- **参数键**: 大写 + 等号，如 `SP=60`, `KP=2.0`, `PT=3s`
- 避免使用单个字母（除 `a`, `b`, `c` 用于临时变量）

### 1.2 代码组织

推荐按以下顺序组织 DCL 程序：

```dcl
# ============================================
# 1. 文件头注释
# ============================================
# 程序描述、作者、日期

# ============================================
# 2. 常量声明
# ============================================
CONST setpoint = 60.0

# ============================================
# 3. 传感器声明
# ============================================
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0

# ============================================
# 4. 信号处理
# ============================================
FILTER temp_f FROM temp LOWPASS a=0.1
PID heater FROM temp_f SP=setpoint KP=2.0 KI=0.1 LIMIT 0 100

# ============================================
# 5. 报警与逻辑
# ============================================
ALARM overheat FROM temp > 80
LOGIC fault = overheat

# ============================================
# 6. 输出声明
# ============================================
OUTPUT heater TO TIM1_CH1
OUTPUT fault TO GPIO_PE0
```

### 1.3 设计原则

1. **数据流优先**: 从传感器到执行器的单向数据流动
2. **信号名即文档**: 使用有意义的信号名，而非 `x`, `y`, `val1`
3. **注释标注意图**: 解释"为什么这么做"，而非"做了什么"
4. **分块组织**: 用注释分隔线（`# === ... ===`）划分逻辑区块
5. **先声明后使用**: 任何信号在被引用前必须先声明

---

## 2. 完整编程示例

### 示例 1: 温度 PID 控制

```dcl
# 温度 PID 控制系统
SENSOR temp FROM ADC1_CH0
FILTER temp_f FROM temp LOWPASS a=0.1
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
ALARM overheat FROM temp_f > 90
OUTPUT heat_pwm TO TIM1_CH1 FROM heater
```

**编译结果:** 6 routes, 4 params, 2 states, 6 wires, 24592 bytes

---

### 示例 2: 带安全连锁的温度控制

```dcl
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

---

### 示例 3: 报警系统

```dcl
# 温度监控与报警
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
ALARM overheat FROM temp > 80
ALARM undertemp FROM temp < 10
LOGIC fault = overheat OR undertemp
OUTPUT fault TO GPIO_PE0
```

---

### 示例 4: 电机启停控制

```dcl
# 电机启停控制（带定时器）
SENSOR start_btn FROM ADC1_CH0 SCALE 1.0 0.0
SENSOR stop_btn FROM ADC1_CH1 SCALE 1.0 0.0
LATCH motor: S1=start_btn, R=stop_btn → Q1=motor_on
TIMER t1: IN=motor_on, PT=3s → Q=running
OUTPUT running TO GPIO_PE0
```

---

### 示例 5: 批次计数

```dcl
# 批次计数 + 定时关停
SENSOR part FROM GPIO_PE3
SENSOR start_btn FROM GPIO_PE4
COUNTER batch: CU=part, PV=100 → Q=batch_done, CV=count
TIMER shutdown: IN=batch_done, PT=2s → Q=stopped
LATCH running: S1=start_btn, R=stopped → Q1=active
OUTPUT motor TO TIM1_CH1 FROM active
```

---

### 示例 6: 手动/自动模式切换

```dcl
# 手动/自动模式切换
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0
SENSOR manual_mode FROM ADC1_CH1 SCALE 1.0 0.0
EQ is_manual FROM manual_mode == 1.0
PID auto_ctrl FROM temp SP=60 KP=2.0 KI=0.1 LIMIT 0 100
MUX output = manual_val SELECT is_manual ELSE auto_ctrl
OUTPUT output TO TIM1_CH1
```
