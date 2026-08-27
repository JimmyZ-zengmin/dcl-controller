# DCL IDE CLI 使用手册

> AI友好的命令行接口
> 版本: 2.0
> 日期: 2026-07-12

---

## 一、概述

DCL IDE CLI 是一个为AI和自动化设计的PLC编程工具，提供完整的DCL程序开发、部署和监控功能。

### 架构变更 (v2.0)

```
v1.0: 每条命令独立进程 → 每次连接pyocd → 执行 → 退出
v2.0: Runtime常驻后台 + HTTP API → CLI/Web/GUI共享连接
```

### 核心组件

| 组件 | 文件 | 用途 |
|------|------|------|
| Runtime | `dcl_runtime.py` | 常驻后台，提供HTTP API |
| CLI | `dcl_cli.py` | 命令行客户端 |
| 编译器 | `dcl_compiler.py` | DCL → 二进制 |
| 硬件 | `core/dcl_hardware.py` | pyocd通信 |

---

## 二、快速开始

### 2.1 启动Runtime后台服务

```powershell
cd d:\STM\work\dcl-controller\ide\compiler
python dcl_runtime.py start
```

输出：
```
[21:03:16] [INFO] Runtime starting...
[21:03:18] [INFO] Hardware connected
[21:03:18] [INFO] HTTP ready :8765
```

### 2.2 完整PLC编程工作流

```powershell
# 1. 新建文件（带模板）
dcl new my_project.dcl

# 2. 编译检查
dcl compile my_project.dcl

# 3. 烧录硬件
dcl deploy my_project.bin

# 4. 读取WIRE数据
dcl wires -s 0 -c 9

# 或者一键执行
dcl execute my_project.dcl
```

---

## 三、命令参考

### 3.1 new — 新建DCL文件

**语法：**
```bash
dcl new <file.dcl> [--force]
```

**参数：**
- `name` — 文件名称（自动添加.dcl后缀）
- `--force` — 覆盖已有文件

**模板内容：**
```
- 注释说明DCL语法
- 传感器定义（SENSOR）
- 参数定义
- 控制逻辑（ARITH/LIMIT/MIN/ABS/EQ/NE）
- 输出定义（OUTPUT）
```

**输出示例：**
```
CREATED: my_project.dcl
Size: 1601 bytes
SUGGEST: edit the file, then compile
```

---

### 3.2 compile — 编译DCL程序

**语法：**
```bash
dcl compile <file.dcl>
```

**输出示例（成功）：**
```
STATUS: OK
routes: 5
params: 4
wires: 5
binary: D:\STM\work\dcl-controller\ide\compiler\my_project.bin
size: 25104 bytes
SUGGEST: deploy <binary>
```

**输出示例（失败）：**
```
STATUS: ERROR
error: 编译失败: 第10: 无法识别的语句: sensor temp 0
```

---

### 3.3 deploy — 部署到硬件

**语法：**
```bash
dcl deploy <file.bin>
```

**输出示例：**
```
STATUS: OK
size: 25104 bytes
routes: 9
SUGGEST: wires s=0 c=9 | monitor
```

---

### 3.4 execute — 一键执行

**语法：**
```bash
dcl execute <file.dcl>
```

**说明：** 编译 + 烧录一步完成。

**输出示例：**
```
STATUS: OK
routes: 9
wires: 9
size: 25104 bytes
SUGGEST: wires s=0 c=9 | monitor
```

---

### 3.5 wires — 读取WIRE值

**语法：**
```bash
dcl wires [-s START] [-c COUNT]
```

**参数：**
- `-s, --start` — 起始索引（默认0）
- `-c, --count` — 读取数量（默认9）

**输出示例：**
```
STATUS: OK
  [0] 0.0
  [1] 0.0
  [2] 0.9999995231628418
  [3] 0.12281317263841629
  [4] 0.0
```

---

### 3.6 status — 系统状态

**语法：**
```bash
dcl status
```

**输出示例：**
```
STATUS: OK
hardware: connected
active_routes: 9
SUGGEST: compile <file> | execute <file>
```

---

### 3.7 monitor — 持续监控

**语法：**
```bash
dcl monitor [-r RATE_MS] [-c COUNT]
```

**参数：**
- `-r, --rate` — 刷新率毫秒（默认200ms）
- `-c, --count` — WIRE数量（默认9）

**输出示例：**
```
Monitoring WIRE (rate: 200ms, Ctrl+C to stop)
--- WIRE Monitor ---
  [2] 0.9999 → 1.0000
```

---

### 3.8 introspect — API发现

**语法：**
```bash
dcl introspect
```

**输出示例：**
```json
{
  "ok": true,
  "apis": ["status", "compile", "deploy", "execute", "wires"]
}
```

---

### 3.9 repl — 交互模式

**语法：**
```bash
dcl repl
```

**可用命令：**
```
new <file>        - 创建新文件
compile <file>    - 编译
deploy <binary>   - 部署
execute <file>    - 编译+部署
status            - 系统状态
wires [s] [c]     - 读取WIRE
monitor [rate]    - 持续监控
introspect        - API列表
runtime [cmd]     - Runtime控制
quit              - 退出
```

---

## 四、Runtime管理

### 启动Runtime

```powershell
python dcl_runtime.py start
```

### 查看状态

```powershell
python dcl_runtime.py status
```

### 停止Runtime

```powershell
python dcl_runtime.py stop
```

### Runtime日志

```
dcl_runtime.log — 运行日志
dcl_runtime.pid — 进程ID
```

---

## 五、Python API

### RuntimeClient类

```python
from dcl_cli import RuntimeClient

client = RuntimeClient("http://localhost:8765")

# 编译
result = client.compile("test.dcl")

# 部署
result = client.deploy("test.bin")

# 读取WIRE
result = client.wires(start=0, count=9)

# 一键执行
result = client.execute("test.dcl")
```

### 返回格式

```python
# 成功
{"ok": True, "routes": 9, "binary": "test.bin", ...}

# 失败
{"ok": False, "err": "错误描述"}
```

---

## 六、DCL语法参考

### 支持的关键字（大写）

| 关键字 | 语法 | 用途 |
|--------|------|------|
| SENSOR | `SENSOR name FROM source [SCALE k b]` | 传感器输入 |
| ARITH | `ARITH name = a OP b` | 算术运算 |
| LIMIT | `LIMIT name FROM src RANGE lo hi` | 限幅 |
| MAX | `MAX name = src MAX value` | 取大 |
| MIN | `MIN name = src MIN value` | 取小 |
| ABS | `ABS name FROM src` | 绝对值 |
| EQ | `EQ name FROM src == value` | 等于 |
| NE | `NE name FROM src != value` | 不等于 |
| OUTPUT | `OUTPUT name TO port` | 输出到端口 |

### OP运算符

- `ADD` — 加法
- `SUB` — 减法
- `MUL` — 乘法
- `DIV` — 除法
- `GT` — 大于
- `LT` — 小于
- `GTE` — 大于等于
- `LTE` — 小于等于

### 示例

```dcl
# 传感器
SENSOR temp FROM ADC1_CH0 SCALE 0.1 0.0

# 温度高报警
ARITH temp_high = temp GT 80.0

# 限幅
LIMIT clamped FROM temp RANGE -10 10

# 输出
OUTPUT temp_high TO GPIO_PE0
```

---

## 七、HTTP API（AI用）

### 基础URL

```
http://localhost:8765
```

### 端点

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/status` | 系统状态 |
| GET | `/api/wires?s=0&c=9` | 读取WIRE |
| GET | `/api/introspect` | API发现 |
| POST | `/api/compile` | 编译 |
| POST | `/api/deploy` | 部署 |
| POST | `/api/execute` | 一键执行 |

### 请求示例

```powershell
# 编译
Invoke-RestMethod -Uri "http://localhost:8765/api/compile" `
  -Method POST `
  -Body '{"file":"test.dcl"}' `
  -ContentType "application/json"

# 读取WIRE
Invoke-RestMethod -Uri "http://localhost:8765/api/wires?s=0&c=9"
```

---

## 八、架构说明

### Runtime与CLI的关系

```
┌────────────────────────────────────────┐
│           DCL Runtime (常驻)            │
│                                         │
│  HTTP API :8765 ◄─── CLI / Web / AI    │
│         │                               │
│         ▼                               │
│  ┌──────────────┐                       │
│  │ pyocd连接池   │ ← 保持连接，快速响应   │
│  └──────────────┘                       │
└────────────────────────────────────────┘
```

### 为什么需要Runtime？

**v1.0问题：**
- 每条命令启动新进程（100ms开销）
- 每次命令重连pyocd（200ms开销）
- 状态不保持

**v2.0优势：**
- Runtime进程常驻（0ms启动开销）
- pyocd连接保持（0ms重连开销）
- 状态持久化

---

## 九、环境配置

### 路径

| 组件 | 路径 |
|------|------|
| CLI | `d:\STM\work\dcl-controller\ide\compiler\dcl_cli.py` |
| Runtime | `d:\STM\work\dcl-controller\ide\compiler\dcl_runtime.py` |
| 编译器 | `d:\STM\work\dcl-controller\ide\compiler\dcl_compiler.py` |
| 核心模块 | `d:\STM\work\dcl-controller\ide\compiler\core\` |
| 示例文件 | `d:\STM\work\dcl-controller\ide\compiler\samples\` |

### Python环境

```
C:\Espressif\tools\python\v6.0.1\venv\Scripts\python.exe
```

### 依赖

- pyocd（硬件通信）
- Python标准库（http.server, json, argparse等）

---

## 十、故障排查

### 问题：连接被拒绝

```
ERROR: Connection refused
```

**解决：**
1. 检查Runtime是否运行：`python dcl_runtime.py status`
2. 启动Runtime：`python dcl_runtime.py start`

### 问题：编译失败

```
STATUS: ERROR
error: 第10: 无法识别的语句: ...
```

**解决：**
1. 检查关键字是否大写
2. 检查语法是否符合规范
3. 参考samples/中的示例文件

### 问题：部署失败

```
STATUS: ERROR
err: no hardware
```

**解决：**
1. 检查USB连接
2. 确认pyocd可用
3. 检查STM32CubeIDE是否占用调试器

---

**文档版本**: 2.0
**作者**: DCL项目组
**日期**: 2026-07-12
