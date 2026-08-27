# IEC 61131-3 对应与扩展方向

---

## 1. IEC 61131-3 标准对应关系

DCL 是 IEC 61131-3 标准的简化文本化实现，以下为标准功能块（FB）到 DCL 关键字的映射：

| IEC 61131-3 标准 FB | DCL 关键字 | 原语操作码 | 验证 |
|---------------------|-----------|-----------|------|
| TON（延时接通） | TIMER mode=TON | TIMER (0x0C) | ✅ |
| TOF（延时断开） | TIMER mode=TOF | TIMER (0x0C) | ✅ |
| TP（脉冲定时） | TIMER mode=TP | TIMER (0x0C) | ✅ |
| CTU（加计数） | COUNTER CU= | COUNTER (0x1D) | ✅ |
| CTD（减计数） | COUNTER CD= | COUNTER (0x1D) | ✅ |
| CTUD（加减计数） | COUNTER CU=,CD= | COUNTER (0x1D) | ✅ |
| SR（置位优先锁存） | LATCH | SR (0x1B) | ✅ |
| RS（复位优先锁存） | RLATCH | RS (0x1C) | ✅ |
| MUX（多路选择） | MUX | MUX (0x08) | ✅ |
| LIMIT（限幅） | LIMIT / PID LIMIT | CLAMP (0x03) | ✅ |
| SCALE（标定） | SCALE / SENSOR SCALE | SCALE (0x0E) | ✅ |
| CMP（比较） | ALARM | CMP (0x01) | ✅ |
| LPF（低通滤波） | FILTER | LPF (0x04) | ✅ |

---

## 2. 当前局限性

| 局限 | 说明 | 影响 |
|------|------|------|
| **无控制流** | 没有 IF/ELSE/FOR/WHILE | 复杂逻辑需要多个 ALARM+LOGIC 组合 |
| **无自定义 FB** | 不能封装可复用的功能块 | 类似程序必须复制粘贴 |
| **无表达式** | 不能写 `a + b * c` | 算术运算需要独立的 ADD/MUL 行 |
| **无数组** | 不支持数组操作 | 批量处理受限 |
| **无赋值** | 没有 `:=` 操作 | 不能直接设置常量 |
| **无枚举/类型** | 只有 float32 | 类型安全为零 |
| **LOGIC 只支持二元** | 只能 A AND B，不能 A AND B AND C | 链式逻辑需要多行 |

---

## 3. 后续扩展候选

| 扩展 | 语法示例 | 编译目标 | 优先级 |
|------|---------|---------|--------|
| 算术表达式 | `ARITH flow = dp * k` | OP_MUL | 高 |
| 多输入逻辑 | `LOGIC ok = a AND b AND c` | 链式 OP_AND | 高 |
| 条件选择 | `MUX output = sel ? a : b` | OP_MUX | 中 |
| 看门狗 | `WATCHDOG timeout=5s → Q=ok` | OP_TIMER | 中 |
| 自定义 FB | `FB MyFilter(inp, alpha) → out { ... }` | 内联展开 | 低 |
| 常量定义 | `CONST pi = 3.14159` | SRC_CONST + param | 低 |
| 数组支持 | `ARRAY[10] temps` | 扩展 WIRE 系统 | 低 |
