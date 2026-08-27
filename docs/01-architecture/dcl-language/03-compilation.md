# 编译流程与二进制格式

> DCL 编译器将源代码编译为二进制路由表，直接写入 STM32H723 DTCM 执行。

---

## 1. 编译流程

```
DCL 源代码
    │
    ├── 1. 预处理: 去除注释（#、//、/* */）
    │
    ├── 2. 逐行解析: 匹配 FB 关键字 → 分配 WIRE/参数/状态
    │      每行生成 1~2 条路由（RouteEntry）
    │
    ├── 3. 拓扑排序: Kahn 算法按 WIRE 依赖关系排序路由
    │      保证: 读取 WIRE[i] 的路由一定在写入 WIRE[i] 的路由之后
    │      检测: 循环依赖 → 编译错误
    │
    ├── 4. 资源验证: 检查路由/参数/状态/WIRE 不超过硬件限制
    │
    └── 5. 生成二进制: 路由表 + 参数表 + CRC32 校验
```

---

## 2. 二进制格式

### 2.1 整体结构

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
│     [9] wire2_idx:   uint16 LE (第二输入 WIRE 索引)      │
├──────────────────────────────────────────────────────────┤
│ Param Table (512 × 16 bytes = 8192 bytes)                │
│   每个参数: value_a, value_b, value_c, value_d (4 × float) │
├──────────────────────────────────────────────────────────┤
│ CRC32 (4 bytes, big-endian)                               │
└──────────────────────────────────────────────────────────┘

总大小 = 12 + 16384 + 8192 + 4 = 24592 bytes（固定）
```

### 2.2 编译示例

```
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100

编译结果:
  WIRE[1] → OP_PID → WIRE[2]    (PID 计算, 输出 heater_pid)
  WIRE[2] → OP_CLAMP → WIRE[3]  (限幅, 输出 heater)

  参数表[0] = {kp=2.0, ki=0.1, kd=0.05, sp=60.0}   (PID 参数)
  参数表[1] = {lo=0.0, hi=100.0, 0, 0}              (CLAMP 参数)
  状态表[0] = heater 的积分项 + 上次误差
```

---

## 3. 操作码全表

| 操作码 | 十六进制 | 有状态 | 对应 FB | 说明 |
|--------|---------|--------|---------|------|
| DIRECT | 0x00 | ❌ | （内部） | 直接传递 |
| CMP | 0x01 | ❌ | ALARM | 比较（> / >= / < / <=） |
| HYST | 0x02 | ✅ | HYST | 滞回比较 |
| CLAMP | 0x03 | ❌ | （内部） | 限幅（PID 自动生成） |
| LPF | 0x04 | ✅ | FILTER | 低通滤波 |
| PID | 0x05 | ✅ | PID | PID 控制器 |
| RATE | 0x06 | ✅ | RATE | 变化率限制 |
| DEADBAND | 0x07 | ✅ | DEADBAND | 死区滤波 |
| MUX | 0x08 | ❌ | MUX | 多路选择 |
| EDGE | 0x09 | ✅ | EDGE | 边沿检测 |
| LUT | 0x0A | ❌ | LUT | 查找表 |
| CNT | 0x0B | ✅ | （内部） | 计数（COUNTER 使用） |
| TIMER | 0x0C | ✅ | TIMER | 定时器（TON/TOF/TP） |
| SCALE | 0x0E | ❌ | SCALE / SENSOR SCALE | 线性标定 y=kx+b |
| AND | 0x0F | ❌ | LOGIC | 逻辑与 |
| OR | 0x10 | ❌ | LOGIC | 逻辑或 |
| NOT | 0x11 | ❌ | LOGIC | 逻辑非 |
| REG | 0x12 | ✅ | LATCH | 寄存器（锁存） |
| ADD | 0x13 | ❌ | ARITH | 加法 |
| SUB | 0x14 | ❌ | ARITH | 减法 |
| MUL | 0x15 | ❌ | ARITH | 乘法 |
| DIV | 0x16 | ❌ | ARITH | 除法 |
| BITAND | 0x17 | ❌ | BIT | 位与 |
| BITOR | 0x18 | ❌ | BIT | 位或 |
| BITXOR | 0x19 | ❌ | BIT | 位异或 |
| BITNOT | 0x1A | ❌ | BIT | 位取反 |
| SR | 0x1B | ✅ | LATCH | SR 触发器（置位优先） |
| RS | 0x1C | ✅ | RLATCH | RS 触发器（复位优先） |
| COUNTER | 0x1D | ✅ | COUNTER | IEC 计数器 |

共 **28 种原语**，其中 **12 种有状态**（需要 state slot）。  
注: DIRECT 和 CLAMP 为内部原语，编译器自动生成，无对应 FB 关键字。

---

## 4. 资源限制

| 资源 | 上限 | 说明 |
|------|------|------|
| 路由数 | ≤ 1024 | 每条 route + 16 bytes |
| 参数块 | ≤ 512 | 每个 param + 16 bytes |
| 状态块 | ≤ 256 | 每个 state + 16 bytes |
| WIRE 数 | ≤ 1024 | 每个 WIRE → 1 个 float32 |
| 二进制总大小 | 24592 bytes | 固定大小（含 CRC） |

---

## 5. 编译器错误排查

### 常见错误

| 错误信息 | 原因 | 解决 |
|----------|------|------|
| `无法识别的语句` | 关键字拼写错误或语法错误 | 检查关键字是否大写，语法是否正确 |
| `第N行解析错误` | 第 N 行语法有误 | 检查该行语句格式 |
| `编译失败: 数据长度不匹配` | 二进制文件损坏 | 重新编译 |

### 调试技巧

1. **逐步添加**: 从最简单的 SENSOR 开始，逐步添加原语
2. **查看符号表**: 编译后检查输出的符号表是否正确
3. **对比参考**: 对比测试文件的写法
