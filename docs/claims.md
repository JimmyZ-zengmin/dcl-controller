# claims —— 契据「可机检主张」登记表

> **本文件是机器读的**（`tools/ref_claims_check.py` 解析下面那个 ```claims 代码块，它已接入
> `build.sh` 作为**第 5 道闸门**）。人类可读的"应该是什么"仍在 `docs/REF-program-contract.md`；
> 本文件是它的**可执行摘要**：每条主张给定一个**机检形式**，机器每次构建都验一遍。
>
> ## 为什么要有它（`docs/PLAN-consistency-v1.md` C 线）
> 2026-09-16 的两轮独立审计共查出 **5 处"契据 vs 实现不一致"**，全部是**人能发现、机器发现不了**的：
> §3.8.5 只写不做 · §3.8.4 里两条规则**字面自相矛盾** · §3.7 未同步令牌 · §3.8.3 漏写
> "接受时也写 `reject=0`" · 工具侧 `expect_len` 只说不做。
> ⇒ **人工审计的成本随规模上升，而"契据写了、代码没做"比"完全没做"更危险**：
> 下一个人会照契据去改，从而**改坏**。本闸门把这件事从人身上搬到机器上。
>
> ## 格式（每个代码块内一行一条）
> ```
> <类> | <字段1> | <字段2> | … [| --allow-uncovered <理由>]
> ```
> ★ **字段用 `|` 分隔**（多词字段必须能表达）。只用空格分隔是退化形式，仅适合单词字段——
>   本项目第一次写这张表就因为空格分隔把多词字段拆错、导致 10 条假红（已作教训记进工具注释）。
>
> | 类 | 字段 | 机检什么 |
> |---|---|---|
> | **A** | `<宏> \| <头文件>` | 该宏在被指文件里 `#define`；**且**（若名字以 `OFF_` 开头）它在 `src/` 某处被 `_Static_assert` 引用 —— 依据 `PLAN-DEV-continuous.md` 执行总则第 5 条 |
> | **B** | `<拒绝码> \| <头文件> \| <验收脚本> \| [证据串]` | ① 定义 ② 在 `src/*.c` 里被**赋值** ③ 脚本里有判据读到它（按**常量名** / 按**数值** / 按**显式证据串**任一）。②③ 不满足必须 `--allow-uncovered <理由>` ⇒ **不允许沉默地留着** |
> | **C** | `<能力位> \| <头文件>` | ① 定义 ② 在 `DCL_CAP_H723_IMPL` 里 ③ **不在** `DCL_CAP_H723_NOTYET`（"宣称 = 实现"）|
> | **E** | `<契据条款> \| <脚本> \| <判据名片段>` | 契据"能失败的判据表"里的每一条，在被指脚本里**真的存在**同名判据（grep `record("…")`）|
> | **N** | `<能力位> \| <头文件>` | **留位**：该位**在** `NOTYET` 里且**不在** `IMPL` 里（"留位 ≠ 实现"要被显式记住）|
> | **C2** | *（自动）* | **完整性**：`transport.h` 里**定义了的**每个 `DCL_CAP_*` 都必须被登记为 **C 或 N**，否则闸门看不见它（"闸门有洞"）|
> | **F** | *（自动）* | **门面/状态一致性**：① 表格行**不得自相矛盾**（现状说"已完成"、状态列写"未完成"）② 提到"实现字/能力字"的行，**末值必须等于** `transport.h` 的 `DCL_CAP_H723_IMPL`。★ 补它的原因：`claims` 只管"契据⇄代码"，**管不到 发布说明/GAP 表/README** —— 同一天真的出现过 6 处过期状态，而**门面写错比没写更坏** |
> | **D** | *（无需登记）* | **全局**扫 `docs/**` 的 `文件:行号`：被引符号是否还在该文件；行号漂移 > 阈值告警；已标"过时/归档"横幅的文档**跳过**（尊重项目自己的标记）|
>
> ★ **覆盖度自报**：每类都有下限。某类登记数低于下限 ⇒ **判据无效**，不是"干净"。

```claims
# ══ A 类：结构（契据/纪律声称存在的符号，必须在代码里真存在）══
A | OFF_SENSOR_MAP     | src/engine.h
A | OFF_TICK_STATS     | src/engine.h
A | OFF_TIMING_OVERRUN | src/engine.h
A | OFF_RTC_DIAG       | src/engine.h
A | OFF_MB_CTRL        | src/engine.h
A | OFF_MACRO_CTRL     | src/engine.h
A | OFF_FAULT_LOG      | src/engine.h
A | OFF_WDT_STAT       | src/engine.h
A | OFF_PERSIST_STAT   | src/engine.h
A | OFF_I2C_XACT       | src/engine.h
A | OFF_DEV_BIND       | src/engine.h
A | OFF_DEV_BIND_SZ    | src/engine.h

# ══ B 类：拒绝码（定义 + 被赋值 + 有判据读到 / 或显式声明）══
# ★ 证据串 = 验收脚本里那句话的**可指认片段**（脚本用自己的码名映射，不写字面常量名 ⇒
#   只按名/按值会误报，而误报会被自己人关掉）。
B | DB_RC_CRC   | src/dev_bind.h | tools/h723_dev_bind_test.py | 坏 CRC 必须被拒
B | DB_RC_DEV   | src/dev_bind.h | tools/h723_dev_bind_test.py | 非法设备码
B | DB_RC_ADDR  | src/dev_bind.h | tools/h723_dev_bind_test.py | addr7=0x80
B | DB_RC_LEN   | src/dev_bind.h | tools/h723_dev_bind_test.py | len=0
B | DB_RC_SEQ   | src/dev_bind.h | tools/h723_dev_bind_test.py   | --allow-uncovered 契约 §3.8.8-3 声称的判据**尚未落地**（见该表 E 类同行）；回退提交会让 done_seq 停低位、rej_n 每圈涨 ⇒ 需要专门设计判据
B | DB_RC_DST   | src/dev_bind.h | tools/h723_dev_bind_test.py   | --allow-uncovered 编码层不可达：dst 仅占 4 bit，掩码后恒 <=15（契约 §3.8.4 末段已如实标注为死码）
B | DB_RC_NOSEQ | src/dev_bind.h | tools/h723_dev_bind_test.py   | --allow-uncovered req_seq==0 在函数入口即 return ⇒ **该常量在 src/*.c 里从未被赋值**，永不出现（契约 §3.8.4 末段）

# ══ C 类：能力位（定义 + 并入 IMPL + 不在 NOTYET）══
C | DCL_CAP_MULTICYCLE | src/transport.h
C | DCL_CAP_HOTRELOAD  | src/transport.h
C | DCL_CAP_PERSISTENT | src/transport.h
C | DCL_CAP_WIRE2_FLAG | src/transport.h
C | DCL_CAP_VERINFO    | src/transport.h
C | DCL_CAP_FORCE      | src/transport.h
C | DCL_CAP_SEQ        | src/transport.h
C | DCL_CAP_COMM       | src/transport.h
C | DCL_CAP_MACRO      | src/transport.h
C | DCL_CAP_AI         | src/transport.h
C | DCL_CAP_DEVBIND    | src/transport.h
C | DCL_CAP_DEVBIND_PERSIST | src/transport.h
C | DCL_CAP_FRAME_V2   | src/transport.h

# ══ N 类：**留位**能力位（定义了但**故意不实现**）—— 必须显式分类, 否则闸门看不见它 ══
# ★ C2 完整性判据要求"每个定义了的位都被显式分类为 已实现(C) 或 留位(N)"。
#   没有 N 类, 这两位就只能被被迫声明成"已实现" —— 那是**让契据说谎**。
N | DCL_CAP_STATE_COLD | src/transport.h
N | DCL_CAP_HMI        | src/transport.h

# ══ E 类：契据 §3.8.8「八条能失败的判据」⇒ 脚本里必须真有 ══
E | 3.8.8-1 magic 在任何清零路径之后仍在    | tools/h723_dev_bind_test.py | RESET 后 IX_MAGIC
E | 3.8.8-1 magic 在任何清零路径之后仍在    | tools/h723_dev_bind_test.py | RESET 后 DB_MAGIC
E | 3.8.8-2 坏 crc 被拒不半装载             | tools/h723_dev_bind_test.py | 坏 CRC 必须被拒
E | 3.8.8-2 坏 crc 被拒不半装载             | tools/h723_dev_bind_test.py | n_valid 仍为 0
E | 3.8.8-4 拒绝也要终止（回 done_seq）      | tools/h723_dev_bind_test.py | 拒绝也要终止
E | 3.8.8-5 读失败保旧值                    | tools/h723_dev_bind_test.py | 仍 == 哨兵
E | 3.8.8-7 在飞期间提交 ⇒ 延后生效          | tools/h723_dev_bind_test.py | SENSOR[15]
E | 3.8.8-8 事务结果归属 ⇒ 不得写别人的槽     | tools/h723_dev_bind_test.py | 用错 lane

# ══ E 类：契约 §3.8.5「绑定表随程序包持久化」（GAP-11，2026-09-16）══
# ★ 每条都**能失败**，而且 T1.2 是**反空判据**（先把活表换成空表 ⇒ "恢复"必须来自段）
E | 3.8.5-1 表从段里恢复（逐字段 == 上传前）| tools/h723_devbind_persist_test.py | 从段里**恢复
E | 3.8.5-2 ★ 反空判据（活表已被换成空表）  | tools/h723_devbind_persist_test.py | 反空判据
E | 3.8.5-3 恢复的表真的在轮询             | tools/h723_devbind_persist_test.py | 计数器在动
E | 3.8.5-4 ★ 老包不凭空多 48 B            | tools/h723_devbind_persist_test.py | 凭空多 48
E | 3.8.5-5 ★ 段坏但程序照常装载           | tools/h723_devbind_persist_test.py | 段坏但**程序照常装载
E | 3.8.5-6 拒绝可观测（load_bad+1/reject）| tools/h723_devbind_persist_test.py | 拒绝可观测
E | 3.8.5-7 重装载幂等（不叠加副作用）      | tools/h723_devbind_persist_test.py | 不叠加副作用

# ══ E 类：GAP-12「帧归属 v2」——“应答里带 CMD+SEQ”这件事必须真被验到 ══
E | 9.4-1 ★ 未协商 ⇒ v1 帧逐字节不变         | tools/h723_frame_attrib_test.py | 逐字节相同
E | 9.4-2 ★ 0x05 的应答必须是 v1（握手可解） | tools/h723_frame_attrib_test.py | 用已知格式解析回执
E | 9.4-3 ★★ 陈旧应答可辨识（反向断言不匹配）| tools/h723_frame_attrib_test.py | 必须不匹配
E | 9.4-4 协商可逆（回到 v1 后逐字节相同）   | tools/h723_frame_attrib_test.py | 回到 v1 之后
E | 9.4-5 序号只回显、不强制顺序             | tools/h723_frame_attrib_test.py | 同一 SEQ
E | 9.4-6 ★ RESET 后模式回 v1（否则链路失联）| tools/h723_frame_attrib_test.py | 用 v1 请求能通
# ★ 以下两条是**契据 §3.8.8 里明确写着、但判据尚未落地**的行。
#   按本项目 DoD（"挂账必须明确写'不做'并给理由，不允许沉默地留着"）**显式登记**，
#   并已在 `docs/REF-program-contract.md` §3.8.8 的同名表里标注"判据未落地"。
E | 3.8.8-3 回退 req_seq 被拒且不回 done_seq | tools/h723_dev_bind_test.py | 序号回退 | --allow-uncovered 判据未落地：构造它需要"合法序 → 回退提交 → 再恢复"三段，且回退会把 done_seq 停在低位（rej_n 每圈涨）⇒ 需专门设计，属 PLAN-consistency-v1 的 P 线
E | 3.8.8-6 只涨 skip_n 不涨 err_n           | tools/h723_dev_bind_test.py | 不涨 err_n | --allow-uncovered 判据未落地：现只观察到"T8.5 聚合"侧面（skip_n 被打印）；要成判据需构造"总线被占满一整段"的受控场景
```
