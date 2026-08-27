# CLI优先开发理念

> 深度智能化时代的软件开发范式
> 版本: v2.1
> 日期: 2026-07-12

---

## 一、核心理念

**软件是AI原生的，CLI是AI的母语。**

在深度智能化时代，软件的第一个用户不是人，而是AI。CLI（命令行界面）是AI操作软件最自然、最可靠的接口。因此，软件开发应当**CLI优先**，GUI后做。

### 与传统开发的区别

```
传统开发:  GUI设计 → 业务逻辑 → 偶尔加个CLI
智能时代:  CLI设计 → 业务逻辑 → GUI作为CLI的可视化外壳
```

### 关键洞察：AI和人类使用CLI的方式完全不同

**人类用CLI**：
```
看到提示符 → 想一个命令 → 敲键盘 → 等结果 → 读输出 → 想下一个命令
```

**AI Agent用CLI**：
```
决策循环：
  1. 观察当前状态（读输出/文件）
  2. 决定下一步（推理）
  3. 执行动作（发送命令）
  4. 回到1（高频、重复）
```

| 人类 | AI Agent |
|------|----------|
| 需要美观的UI | 只需要结构化数据 |
| 喜欢即时反馈 | 需要可编程解析的输出 |
| 一次做一个任务 | 需要批量/链式操作 |
| 手动读大量输出 | 需要精准提取关键信息 |
| 记住上次做了什么 | 需要系统自动维护上下文 |
| 敲命令很慢 | 命令往返是主要瓶颈 |

---

## 二、三条铁律

### 铁律一：CLI优先

- **先写CMD/PowerShell版本**，验证成功后再开发图形界面
- CLI是软件的"骨架"，GUI是"皮肤"
- 所有功能必须先在CLI中实现并验证，才能进入GUI
- 很多AI Agent本身就是控制台CLI程序，我们的软件必须能被它们直接调用

### 铁律二：CLI与GUI完全同步

- CLI中执行的每一个操作，必须**实时反映**在GUI界面上
- GUI中执行的每一个操作，必须**可被CLI复现**
- 不能出现"AI在CLI上能做、但在GUI上做不到"的情况，反之亦然
- 同步是双向的、实时的、无损的

### 铁律三：多会话隔离

- 支持多个CLI实例同时连接同一个软件
- 每个CLI实例是独立的**工作会话**
- 一个会话的操作**不应干扰**另一个会话的工作
- GUI本身也是一个会话，与CLI会话平等

---

## 三、面向AI的CLI设计原则

### 原则一：最小化命令往返次数

**问题：每条命令启动新进程 + 重连pyocd = 300ms开销**

**解决方案：Runtime常驻后台 + HTTP API**

```powershell
# v1.0问题
dcl compile test.dcl        # 启动进程(100ms) → 连接pyocd(200ms) → 编译 → 退出
dcl deploy test.bin         # 启动进程(100ms) → 连接pyocd(200ms) → 写入 → 退出

# v2.0方案
dcl compile test.dcl        # HTTP调用 → 立即返回
dcl deploy test.bin         # HTTP调用 → 立即返回
```

### 原则二：输出是给机器解析的，不是给人看的

AI友好的输出格式：
```
# 成功
STATUS: OK
ROUTES: 9
BINARY: test.bin
SUGGEST: deploy

# 失败
STATUS: ERROR
CAUSE: 第10行语法错误
FIX: 使用大写关键字
RETRY: dcl compile test.dcl
```

### 原则三：持久化连接+事件流

AI Agent最痛苦的事：每个命令都要重新启动进程、连接pyocd。

**解决方案：Runtime常驻 + pyocd连接保持**

```
┌─────────────────────────────────────────────────────┐
│  DCL Runtime (常驻后台)                              │
│                                                      │
│  ┌──────────┐  ┌──────────────────┐                 │
│  │ HTTP API │  │ pyocd连接池       │                 │
│  │ :8765    │  │ (保持连接)        │                 │
│  └────┬─────┘  └────────┬─────────┘                 │
│       └─────────────────┘                            │
└─────────────────────────────────────────────────────┘
```

**关键优势**：
- pyocd连接只建立一次
- 进程启动开销为零
- AI可以订阅事件，而不是主动查询

---

## 四、DCL Runtime 架构（已实现）

### 4.1 当前实现

```
┌──────────────────────────────────────────────┐
│              DCL Runtime (已实现)             │
│                                               │
│   CLI层（人类用）  │  API层（AI用）            │
│   ──────────────  │  ──────────              │
│   $ dcl shell     │  POST /api/compile       │
│   > compile       │  POST /api/deploy        │
│   > deploy        │  GET  /api/wires          │
│   > wires         │  POST /api/execute       │
│   > new           │  GET  /api/status        │
│   > monitor       │                          │
│                   │                          │
│   ──────────────  │  ──────────              │
│        └──────────┴──────────┘               │
│                     │                         │
│              ┌──────▼──────┐                  │
│              │  核心引擎    │                  │
│              └─────────────┘                  │
└──────────────────────────────────────────────┘
```

### 4.2 已实现组件

| 组件 | 文件 | 状态 |
|------|------|------|
| Runtime主进程 | `dcl_runtime.py` | ✅ 完成 |
| HTTP API服务器 | `dcl_runtime.py` (内嵌) | ✅ 完成 |
| 编译器封装 | `core/dcl_compiler.py` | ✅ 完成 |
| 硬件通信 | `core/dcl_hardware.py` | ✅ 完成 |
| CLI客户端 | `dcl_cli.py` | ✅ 完成 |
| 编译器核心 | `dcl_compiler.py` | ✅ 完成 |

### 4.3 已实现命令

| 命令 | 功能 | AI优化 |
|------|------|--------|
| `new <file>` | 新建DCL文件（带模板） | 一键创建 |
| `compile <file>` | 编译检查 | 快速反馈 |
| `deploy <bin>` | 部署到硬件 | 立即执行 |
| `execute <file>` | 编译+部署 | 一步完成 |
| `wires -s 0 -c 9` | 读取WIRE值 | 简洁输出 |
| `status` | 系统状态 | 即时查询 |
| `monitor` | 持续监控 | 实时更新 |
| `introspect` | API发现 | AI自发现 |
| `repl` | 交互模式 | 批量操作 |

---

## 五、HTTP API（AI用）

### 基础URL

```
http://localhost:8765
```

### 端点

| 方法 | 路径 | 用途 | 请求体 |
|------|------|------|--------|
| GET | `/api/status` | 系统状态 | — |
| GET | `/api/wires?s=0&c=9` | 读取WIRE | — |
| GET | `/api/introspect` | API发现 | — |
| POST | `/api/compile` | 编译 | `{"file":"test.dcl"}` |
| POST | `/api/deploy` | 部署 | `{"binary":"test.bin"}` |
| POST | `/api/execute` | 一键执行 | `{"file":"test.dcl"}` |

### Python客户端

```python
from dcl_cli import RuntimeClient

client = RuntimeClient("http://localhost:8765")

# 编译
result = client.compile("test.dcl")
if result["ok"]:
    print(f"编译成功: {result['routes']}条路由")
else:
    print(f"编译失败: {result['err']}")

# 部署
result = client.deploy("test.bin")

# 读取WIRE
result = client.wires(start=0, count=9)
```

---

## 六、开发进度

### Phase 1: Runtime核心 ✅ 已完成

- [x] 常驻守护进程 (dcl_runtime.py)
- [x] HTTP API服务器 (内嵌)
- [x] 编译器封装 (core/dcl_compiler.py)
- [x] 硬件通信 (core/dcl_hardware.py)

### Phase 2: CLI外壳 ✅ 已完成

- [x] 命令行入口 (dcl_cli.py)
- [x] 交互式REPL (repl模式)
- [x] 命令补全和帮助

### Phase 3: AI接口 🔄 进行中

- [x] HTTP API客户端 (RuntimeClient类)
- [ ] WebSocket事件订阅（待开发）
- [ ] 声明式能力描述（待开发）
- [ ] 智能错误修复建议（待开发）

### Phase 4: GUI (Web IDE) ⏳ 待开发

- [ ] 复用Runtime后端
- [ ] 可视化编辑器
- [ ] 监控面板
- [ ] 多会话UI

---

## 七、验证状态

### 基础验证（全部通过）

| # | 验证项 | 状态 |
|---|--------|------|
| 1 | CLI可以操作 | ✅ |
| 2 | CLI操作结果正确 | ✅ |
| 3 | Runtime稳定运行 | ✅ |
| 4 | HTTP API正常响应 | ✅ |

### AI友好验证

| # | 验证项 | 状态 |
|---|--------|------|
| 5 | HTTP API可用 | ✅ |
| 6 | 批量执行正常 | ✅ |
| 7 | 输出格式简洁可解析 | ✅ |
| 8 | 错误信息包含修复建议 | ⚠️ 基础版 |
| 9 | 系统能力可自动发现（introspect） | ✅ |
| 10 | 状态可查询（无副作用） | ✅ |

### 完整工作流验证（通过）

```powershell
# 新建文件 → 编译 → 部署 → 监控
dcl new my_project.dcl       # ✅ 创建模板
dcl compile my_project.dcl   # ✅ 编译检查
dcl deploy my_project.bin    # ✅ 烧录硬件
dcl wires -s 0 -c 9          # ✅ 读取数据
```

---

## 八、总结

> 这不是一个"先做CLI凑合用"的妥协方案。
> 这是一个**以CLI为核心、以AI为第一用户**的软件架构哲学。
>
> 在这个理念下，软件天然就是可编程的、可自动化的、可被AI操控的。
> GUI不是软件本身，而是软件的一面"镜子"——让你看到CLI正在做什么。
>
> 面向AI的CLI，本质上应该是**带CLI外壳的本地API服务**：
> - 对人类：CLI提供shell模式、自动补全、状态显示
> - 对AI：HTTP/WebSocket提供持久连接、结构化输出、事件推送
>
> 这样AI的每次操作从"启动进程→连接pyocd→执行→断开"
> 变成"HTTP调用→立即返回"，效率提升100倍。

---

**文档版本**: v2.1
**作者**: 项目组
**日期**: 2026-07-12
