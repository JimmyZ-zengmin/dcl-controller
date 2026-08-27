# IDE→编译→烧录→执行 完整流程验证计划

> 目标：验证DCL IDE工具链的端到端可用性
> 日期：2026-07-12
> 状态：待执行

---

## 一、验证目标

用IDE写一个DCL程序，编译成二进制，加载到H723硬件，验证ISR执行结果正确。

**核心问题**：当前工具链是否能支撑"写代码→编译→烧录→运行"的完整闭环？

---

## 二、当前工具链状态

### 2.1 已有组件

| 组件 | 路径 | 功能 | 状态 |
|------|------|------|------|
| DCL编译器 | `ide/compiler/dcl_compiler.py` | DCL语法→二进制路由表 | ✅ 可用（支持28种原语） |
| 加载工具 | `ide/compiler/load_dcl.py` | 二进制→pyocd写入DTCM | ✅ 可用 |
| Web IDE | `ide/web/` | 编辑器+编译器+USB通信 | ⚠️ 框架有，未完整测试 |
| 固件 | `firmware/h723-core0/` | ISR引擎（34种原语） | ✅ 已验证 |

### 2.2 差距分析

| 问题 | 影响 | 优先级 |
|------|------|--------|
| 编译器只支持28种原语，固件已升级到34种 | 无法使用LIMIT/MAX/MIN/ABS/EQ/NE | **P0** |
| load_dcl.py通过pyocd逐条write32，速度慢 | 加载100条路由需数秒 | P1 |
| Web IDE未做端到端测试 | 不确定是否能正常工作 | P1 |

---

## 三、验证步骤

### Step 1: 更新编译器支持34种原语

**文件**：`ide/compiler/dcl_compiler.py`

**修改内容**：
```python
# 在OP_MAP中添加6个新原语
OP_MAP = {
    # ... 现有28种 ...
    'LIMIT':   0x1E,
    'MAX':     0x1F,
    'MIN':     0x20,
    'ABS':     0x21,
    'EQ':      0x22,
    'NE':      0x23,
}
```

**验证**：编译一个包含新原语的DCL程序，检查生成的二进制是否正确。

---

### Step 2: 编写测试DCL程序

**文件**：`ide/compiler/samples/test_new_primitives.dcl`

```dcl
# 测试新增6种原语
# 输入：ADC1_CH0 → SENSOR[0]

# 信号调理
SENSOR  temp     FROM ADC1_CH0    SCALE 1.0 0.0

# 测试新原语
LIMIT   clamped  FROM temp        RANGE -10 10      # 限幅到[-10, +10]
MAX     max_val  = temp MAX 5.0                     # 取max(temp, 5.0)
MIN     min_val  = temp MIN 3.0                     # 取min(temp, 3.0)
ABS     abs_val  FROM temp                          # 绝对值

# 比较测试
ALARM   is_five  FROM temp == 5.0                   # 等于5.0
ALARM   not_five FROM temp != 5.0                   # 不等于5.0

# 输出到WIRE（便于pyocd读取验证）
OUTPUT  clamped  TO WIRE[110]
OUTPUT  max_val  TO WIRE[111]
OUTPUT  min_val  TO WIRE[112]
OUTPUT  abs_val  TO WIRE[113]
OUTPUT  is_five  TO WIRE[114]
OUTPUT  not_five TO WIRE[115]
```

**预期**：
- 编译成功，生成`test_new_primitives.bin`
- 包含6条路由，对应6个新原语

---

### Step 3: 编译生成二进制

**命令**：
```bash
cd ide/compiler
python dcl_compiler.py samples/test_new_primitives.dcl -o samples/test_new_primitives.bin
```

**验证**：
- 编译无错误
- 生成二进制文件
- 用`--json`参数查看路由表结构，确认6条路由

---

### Step 4: 加载到硬件

**前提**：H723固件已烧录（包含ISR引擎）

**命令**：
```bash
cd ide/compiler
python load_dcl.py samples/test_new_primitives.bin
```

**验证**：
- pyocd成功写入ROUTE_TABLE和PARAM_TABLE
- ACTIVE_ROUTES设置为6

---

### Step 5: 验证执行结果

**命令**：
```bash
# 读取WIRE[110~115]
pyocd commander -t stm32h723xx -c "read32 0x200004B8 24; exit"
```

**预期结果**（假设ADC输入temp=2.5V）：
| WIRE | 原语 | 预期值 |
|------|------|--------|
| 110 | LIMIT(-10, 2.5, +10) | 2.5 |
| 111 | MAX(2.5, 5.0) | 5.0 |
| 112 | MIN(2.5, 3.0) | 2.5 |
| 113 | ABS(2.5) | 2.5 |
| 114 | EQ(2.5, 5.0) | 0.0 |
| 115 | NE(2.5, 5.0) | 1.0 |

**验证方法**：
1. 读取WIRE值，转换为float
2. 对比预期值
3. 多次读取，确认ISR持续执行（值应稳定）

---

## 四、成功标准

| 标准 | 验证方法 |
|------|----------|
| 编译器支持34种原语 | 编译test_new_primitives.dcl无错误 |
| 加载工具正常工作 | pyocd成功写入DTCM |
| ISR执行新原语 | WIRE[110~115]输出正确 |
| 端到端流程可用 | 从DCL代码到硬件执行，全程自动化 |

---

## 五、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 编译器不支持新原语语法 | 无法编译 | Step 1先更新编译器 |
| load_dcl.py写入失败 | 无法加载 | 检查pyocd连接，重试 |
| ISR不执行新路由 | 输出为0 | 检查ACTIVE_ROUTES设置，检查固件版本 |
| 新原语实现有bug | 输出错误 | 对比C代码测试（已验证通过） |

---

## 六、后续扩展

验证通过后，可以：

1. **完善Web IDE**：集成Monaco编辑器+编译按钮+USB加载
2. **添加更多原语**：SEL、MOV、整数运算等
3. **支持功能块实例化**：PID1、PID2多个实例
4. **多速率任务**：快环100μs，慢环1ms
5. **IEC 61131-3标准兼容**：数据类型、POU封装

---

## 七、执行清单

- [x] Step 1: 更新编译器支持34种原语
- [x] Step 2: 编写test_new_primitives.dcl
- [x] Step 3: 编译生成二进制
- [x] Step 4: 加载到硬件
- [x] Step 5: 验证执行结果
- [x] 记录问题，修复bug
- [x] 更新文档

---

## 八、验证结果（2026-07-12）

### 实际执行结果

**ADC输入**: 1.2284V

| WIRE | 原语 | 输入 | 预期 | 实际 | 结果 |
|------|------|------|------|------|------|
| 0 | SENSOR | ADC1_CH0 | - | 1.2284 | - |
| 1 | **LIMIT** | 1.2284, [-10, 10] | 1.2284 | 1.2284 | ✅ |
| 2 | **MAX** | 1.2284, 5.0 | 5.0 | 5.0000 | ✅ |
| 3 | **MIN** | 1.2284, 3.0 | 1.2284 | 1.2284 | ✅ |
| 4 | **ABS** | 1.2284 | 1.2284 | 1.2284 | ✅ |
| 5 | **EQ** | 1.2284, 5.0 | 0.0 | 0.0000 | ✅ |
| 6 | **NE** | 1.2284, 5.0 | 1.0 | 1.0000 | ✅ |

### 结论

✅ **端到端验证成功**

- 编译器支持34种原语（新增LIMIT/MAX/MIN/ABS/EQ/NE）
- DCL程序编译通过，生成9条路由
- load_dcl.py成功加载到硬件
- ISR正确执行所有新原语，结果与预期一致

### 修改文件

1. `ide/compiler/dcl_compiler.py` - 添加6个新原语的OP_MAP和解析方法
2. `firmware/h723-core0/Src/main.c` - 添加6个新原语的C实现
3. `ide/compiler/samples/test_new_primitives.dcl` - 测试程序

### 下一步

- 同步代码到D:\STM\work\core0_h723
- 完善Web IDE界面
- 添加更多原语（SEL/MOV/SQRT等）

---

**文档版本**: v1.1
**维护者**: 项目组
**最后更新**: 2026-07-12
