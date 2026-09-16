#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""h723_devbind_persist_test.py — **具名设备绑定表随程序包持久化** 上机验收

契据: `docs/REF-program-contract.md` §3.8.5（GAP-11）。
实现侧: `src/dev_bind.c` 的 `dev_bind_pack()/dev_bind_unpack()`、
        `src/main.c` 的 `h_prog_commit()` / `prog_boot_load_apply()` / `h_pin_pattern()`，
        SHM 布局 `src/engine.h`（`OFF_DEV_BIND` 段）、能力位 `src/transport.h`。

═══════════════════════════════════════════════════════════════════════════
## 0. `0x39 op=23` 为什么存在（★ 这段理由必须随工具一起被读到）
═══════════════════════════════════════════════════════════════════════════
本脚本要验的是"绑定表随程序包持久化"，而"持久化"的字面验收动作是**断电重上电**。
但真断电需要人拔插，**无法自动化**，于是固件提供 `0x39`(`CMD_PIN_PATTERN`) `op=23`：
**重新执行开机装载（程序 + 绑定表）**，应答 8 B = `[rc:u32][loaded:u32]`。

它之所以能代替"开机"来做验收，**只因为**它调的是 `prog_boot_load_apply()`
—— 与 `main()` 启动序列**同一个函数**（`src/main.c` 的注释：`@@` "若它与开机各写一份，
那验的就不是开机路径了"）。本脚本的 T1/T3/T4/T5 全部走这条口。

★★ 但这句话必须说清楚：**本脚本验的是"装载路径"，不是"断电"**。
   `op=23` 不经过 `sd_init()` / 复位 / 掉电时序，所以下列风险它**覆盖不到**：
     · 掉电瞬间写入被中断（A/B 双副本 + 头最后写 是那条风险的处置，另有专门套件）
     · 上电时序（SD 初始化早于装载）—— 本脚本只验"装载函数被调用后的行为"
   ⇒ **真断电验证仍需人工做一次**，做法见本文件的 `--keep` / `--verify-only`（见 §5）。

═══════════════════════════════════════════════════════════════════════════
## 1. 段格式与"谁打包"——★ 以源码为准，与任务书的口述有出入
═══════════════════════════════════════════════════════════════════════════
段（48 B，载荷**尾部**，仅当设备侧确有绑定表时才附加）:
    [0..3]  magic 'DBND' = 0x444E4244 (LE)     [4..7]  seg_len = 48
    [8..11] period (u32 1..1000)               [12..15] fnv = FNV-1a **按 32 位字** over entries[8]
    [16..47] entries[8] (u32 ×8)
载荷 = `[6B counts][routes][params][states][48B 段(可选)]`

★ **打包方 = 固件，不是上位机**（这一点与任务书的口述不同，以源码为准）:
  `src/main.c: h_prog_commit()` 在 CRC32（闸2）与 `prog_validate`（闸5）**之后**调用
  `dev_bind_pack(prog_store_buf(), s_txn_total, ...)`，把**设备当前**的表塞到载荷尾部，
  再交给 `prog_store_save()` 落盘（副本头里的 len/crc32 按**附加后**的长度算）。
  `dev_bind_pack()` 的第一句就是 `if (s_n == 0u) return len;` —— 无绑定 ⇒ 不附加空段。

  这条事实决定了三条判据怎么构造，**不能照任务书的字面去构造**:
    · T1「上传含段的包」= 上位机上传**裸程序**，同时**保证设备侧有活表**
      ⇒ 段由固件附加。判"含段"的证据 = `0x48` 的**实际存储长度 == 裸长 + 48**。
    · T3「不含段的老格式包」= 上位机**先把表清空**（`s_n==0`）再上传裸程序
      ⇒ 固件不附加 ⇒ 长度必须**恰好**等于 `6+(nr+np+ns)*16`。
    · T4「段内 fnv 被改坏」★ **上位机无法让固件产出一个坏 fnv** ⇒ 只能在
      **表为空**（固件不附加）时，由上位机**自己把 48 B 假段拼在载荷尾部**发上去，
      固件照原样落盘；装载时尾部的 48 B 就是那个坏段。这是 T4 唯一可构造的形态。

★ fnv 算法 = `db_crc()`（`src/dev_bind.c`）: `h=2166136261; for w in entries[8]: h=(h^w)*16777619`
  —— **逐个 u32 字**，不是"摊成 32 个 LE 字节"。本脚本 `db_crc_words()` 与它逐字同构。

═══════════════════════════════════════════════════════════════════════════
## 2. 判据（每条都**能失败**，且写明了什么条件下会红）
═══════════════════════════════════════════════════════════════════════════
  0) 前置
  T0.1  链路活（`0x01` 帧合法且 CRC 通过）
  T0.2  ★ `0x01` 的能力字含 `DCL_CAP_DEVBIND_PERSIST`（**值从 src/transport.h 解析，不写死**）
        红: 老固件/未声明该位 ⇒ **判「前置不成立」并退出 1**（**不是 PASS**）
  T0.3  与源码对账: 工具内 24 个常量（SHM 偏移/段长/拒绝码/命令码）必须等于 `src/*.h` 解析值
        红: 任一处不等 ⇒ 退出 1（此时读到的每个数都不可信，继续跑等于生产假结论）
  T0.4  `0x38` 可读且 shm 基址非 0（后续一切读数都建在它上面）
  T0.5  ★ SHM 自证 `DB_MAGIC == 'DBND'`（证明本脚本读的偏移就是固件写的那块）
  T0.6  `0x48` 可读且长度恰为 96 B
  T0.7  `0x48.part_ok == 1`（SD 已分区 ⇒ 程序存储可用）

  1) T1 = 有活表时上传 ⇒ 固件必须附加段，重装载必须恢复
  T1.0  表 A 被接受（`dev_bind_pack` 的前提 `s_n != 0`）+ 上传裸程序回 `rc==0`
  T1.1  ★ 落盘长度 == 裸长 + 48（固件确实附加了段）
        红: 固件不附加（长度 == 裸长）
  T1.2  ★ 反空判据: 上传之后、重装载之前，本脚本**故意把活表换成空表 + 别的 period**
        红: 这一步没做到 ⇒ 后面"恢复成功"可能只是"表一直没被清掉"
  T1.3  `op=23` 受理且 `rc == PROG_RC_OK`
        红: NAK / TIMEOUT / rc!=0
  T1.4  ★ `n_valid` / `period` / `entries[8]` **逐字段**等于上传前的表 A
        红: 固件没实现恢复 ⇒ `n_valid==0`（= 停留在 T1.2 换上去的空表）
  T1.5  `DB_LOAD_OK_N` 恰 +1（恢复被记数 ⇒ 走的是恢复路径，不是别的巧合）

  2) T2 = 恢复的表**真的在被轮询**（把"字段对了"与"生效了"分开）
  T2.1  ★ `ok_n` 或 `err_n` 在涨
        红: 只恢复了字段、`dev_bind_service()` 没跑 ⇒ 两个都不动
  T2.2  分类: `ok_n` 涨 ⇒ **器件在场**；只有 `err_n` 涨 ⇒ **未接器件**
        （`last_err == I2C_SM_ST_NAK(3)` 是未接器件的**合理**结果 ⇒ PASS 并明确标注）
        红: `err_n` 在涨但 `last_err` 既不是 NAK 也不是 OK（那是器件/接线问题，不是持久化问题）

  3) T3 = 老格式包（无段）必须**不被**附加、**不被**恢复
  T3.0  先把活表清空（`s_n==0`）再上传裸程序
  T3.1  ★ 落盘长度 == `6+(nr+np+ns)*16`（**没有**凭空多 48 B）
        红: 固件无条件附加段 ⇒ 多 48 B
  T3.2  `op=23` 后程序**照常装载**（rc==0 且 `deploy_seq` 增 ≥1）
  T3.3  ★ `n_valid == 0`（老包 ⇒ 不恢复）
        红: 读了**较旧**的那份带段副本 ⇒ 表被恢复
  T3.4  `DB_LOAD_OK_N` / `DB_LOAD_BAD_N` **都不动**（无段 ⇒ 既不恢复也不拒绝）
        红: 无段也计"恢复成功"⇒ 那个计数再也无法回答"有没有恢复"

  4) T4 = 段存在但 fnv 被改坏 ⇒ 段被拒、程序照装、拒绝**可观测**
  T4.0  清空活表 + 上位机**自己**拼一个坏段追加在载荷尾部；落盘长度 == 裸长 + 48
        红: 固件又附加了一段 / 截断了
  T4.1  段坏但**程序照常装载**（rc==0 且 `deploy_seq` 增 ≥1）
        红: 段坏牵连程序 ⇒ 程序也拒绝装载
  T4.2  `n_valid` 保持原值（本例=0，**不半装载**）
        红: 固件不校验 fnv ⇒ 坏表被接受并生效 ⇒ `n_valid=2`
  T4.3  `entries[8]` 不被写成半张表（全 0）
        红: 写了前几个字就退出
  T4.4  ★ 拒绝**可观测**: `DB_LOAD_BAD_N` +1 且 `DB_REJECT == DB_RC_CRC`
        红: 静默拒绝（任何观测量都不变）

  5) T5 = 重复重装载**幂等**（不叠加副作用）
  T5.0  表 A 就位且包内含段（前置）
  T5.1  连发两次 `op=23` ⇒ 两次 `rc` 相同且都为 OK
  T5.2  ★ 两次的 `n_valid`/`period`/`entries[8]` 完全一致 **且**等于表 A
  T5.3  `req_seq` 单调不减 且 `done_seq` 追上它（第二次没把序号搞乱/卡死）
  T5.4  两次都无拒绝（`reject==0` 且 `rej_n` 不变）
  T5.5  两次之后表**仍在**被轮询（`ok_n`/`err_n` 还在涨）
  T5.6  ★ `DB_LOAD_OK_N` 至少 +2（每次 `op=23` 都各记一次恢复）

  6) 离线自检（`--dry-run`，不碰串口）
  D0  解析 `src/*.h`，且能力位是**解析出来**的（不是写死的）
  D1  段构造逐字段自检（magic/seg_len/period/fnv/entries[8] == 48 B）
  D2  ★ T1/T3/T4 的载荷分别落在 ok/none/bad 三个分支（不是同一个实验跑三遍）
  D3  老包载荷尾部 48 B 不以 'DBND' 开头（T3 不会被误判成"有段"）
  D4  上传分片恰好覆盖 `[0,total)` 一次、offset 连续（`0x46` 的 `off != got` 会被拒）
  D5  长度算式自洽: 裸长 = `6+(nr+np+ns)*16`；含段 = 裸长 + 48
  D6  ★ 自毁测试: 故意把工具常量改错一处 ⇒ T0.3 **必须**报红（证明它不是恒真判据）
  D7  ★ 自毁测试: 段判定三态可移（改 entries⇒bad / 破 magic⇒none / 原样⇒ok）

★ 与"设备真的被读到"有关的只有 T2。**不假定板子接了 AS5600**：接了走 `ok_n`，
  没接走 `err_n`（NAK 是合理结果），两条都判，且报告里会写明是哪一种。
★ 目标 SENSOR 槽用 **7 / 11**，不用 `SENSOR[0]/[1]`（AS5600 的 RAW/DEG，10 ms 一次被覆写），
  也不用 `SENSOR[15]`（已有别的判据在用）。理由与 `tools/h723_dev_bind_test.py` 文件头一致。

═══════════════════════════════════════════════════════════════════════════
## 2.5 ★ 一处**只有跑过才暴露**的坑: `dev_bind_unpack()` 会 `DB_REQ_SEQ += 1`
═══════════════════════════════════════════════════════════════════════════
`src/dev_bind.c` 的 `dev_bind_unpack()` 在把段写进 SHM 之后，**又把它当一次"表上传"**
走了一遍 `dev_bind_submit()` 的校验，并把 `DB_REQ_SEQ` **加 1**。
而 `dev_bind_submit()` 的受理条件是 `req_seq != done_seq` 且递增
（`(int32_t)(rq - dn) < 0` ⇒ 直接 `DB_RC_SEQ`，**且拒绝也会写 `done_seq`**）。

后果（本脚本第一版就栽在这里，**硬件上完全没有报错征兆**）:
  · 若上位机自己维护一个本地序号计数器，**第一次 `0x39 op=23` 之后它就会与设备撞号**;
  · 撞号 ⇒ `req_seq == done_seq` ⇒ 固件的"已经处理过"分支**静默 return**（不报错、不改任何计数）;
  · 于是 T3 的"清空表"变成**静默空转**，之后每一条判据都在**旧表**上评判 ⇒ **假 PASS**。
⇒ 本脚本的对策（`submit()`）: 序号基准**从设备读**（`seq = max(本地, DB_REQ_SEQ) + 1`），
  并且**不采信** `req_seq` 与自己写进去的值对不上的那张快照（宁可超时也不误判为成功）。
  这同时是一条对**上位机/评审者**的规矩: **提交前必须让 `req_seq` 严格大于设备当前的 `done_seq`**。

═══════════════════════════════════════════════════════════════════════════
## 3. 退出码（"判据没跑到"必须与"通过"区分开）
═══════════════════════════════════════════════════════════════════════════
  0 = 全部判据通过（无 SKIP）
  2 = 有 FAIL
  1 = 环境错误 / **前置不成立** / 有判据**未跑到**（SKIP）

═══════════════════════════════════════════════════════════════════════════
## 4. 副作用（★ 会覆盖设备上已有的程序）
═══════════════════════════════════════════════════════════════════════════
  · 本脚本会用 `0x45/0x46/0x47` **覆盖**设备上现有的持久化程序（2 条 CONST→WIRE 的路由）。
  · 默认收尾: 提交空表（`n_valid=0`）+ `0x49 ERASE`（清掉本次上传的程序）。
  · `--keep`: **不擦**，并把"程序 + 回读到的表"留在设备上，便于人工做**真断电**验证；
    收尾时打印期望值与一条可直接粘贴的 `--verify-only` 命令。
  · 串口是**独占资源**。跑之前请确认没有别的进程在跑（本项目的两个 CH340 通到同一块板子，
    并发抢口会造成"板子没响应"这种**看起来像硬件坏了**的假象）。

用法:
    python tools/h723_devbind_persist_test.py --dry-run          # 离线自检（不碰串口）
    python tools/h723_devbind_persist_test.py --port COM21
    python tools/h723_devbind_persist_test.py --port COM21 --keep
    # ↓ 人工真断电重上电**之后**再跑:
    python tools/h723_devbind_persist_test.py --verify-only --expect-nvalid 2 --expect-period 5 \
        --expect-entries 0x18070C36,...
"""
import argparse
import os
import re
import struct
import sys
import time
import zlib

# ★ Windows 控制台默认 GBK: 个别字符（⇒ / ✅ 等）会让脚本在**打印结论**那一步抛
#   UnicodeEncodeError 而崩掉 —— 数据都量到了却看不到。统一改成"永不抛"。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SRC = os.path.join(_ROOT, "src")

# ══════════════════ 协议常量（src/transport.h）══════════════════
C_VERSION = 0x01
C_WRITE = 0x21          # [addr:u32][value:u32]        → ACK 4B
C_READ_BURST = 0x22     # [addr:u32][count:u16]        → ACK count*4B
C_WRITE_BURST = 0x23    # [addr:u32][count:u16][data]  → ACK 4B
C_ENGINE_STATUS = 0x38  # → ACK ENG_STATUS_LEN(51)B
C_PIN_PATTERN = 0x39    # [op:u8][sub:u8][arg:u32] —— op=23 = 重执行开机装载
C_PROG_BEGIN = 0x45
C_PROG_DATA = 0x46
C_PROG_COMMIT = 0x47
C_PROG_STATUS = 0x48
C_PROG_ERASE = 0x49

OP_RELOAD = 23          # 0x39 op=23（见 §0）
ENG_STATUS_LEN = 51     # src/main.c: #define ENG_STATUS_LEN 51u

# i2c_sm 状态码（src/i2c_sm.h）—— 只用来给"未接器件"这一路做归因
I2C_SM_ST_NAK = 3
I2C_SM_ST_NAME = {0: "IDLE", 1: "BUSY", 2: "OK", 3: "NAK",
                  4: "SCL_STUCK", 5: "BADARG", 6: "GATE_BUSY"}

# 目标器件: AS5600 RAW ANGLE（契约 §3.8.2 的具名例子）
AS5600_ADDR7, AS5600_REG, AS5600_LEN = 0x36, 0x0C, 2
DST_A, DST_B = 7, 11    # ★ 见 §1 末; 不用 0/1（AS5600 覆写）/ 15（别人在用）

# ══════════════════ 工具侧的常量（★ 会与 src/*.h 逐项对账, 见 t0_static）══════════════════
CAP_BIT_DEFAULT = None  # 不写死: 由 src/transport.h 解析（或用 --cap-bit 覆盖）

OFF_DEV_BIND = 0x7300
DB_MAGIC = OFF_DEV_BIND + 0
DB_N_VALID = OFF_DEV_BIND + 4
DB_CRC = OFF_DEV_BIND + 8
DB_REQ_SEQ = OFF_DEV_BIND + 12
DB_DONE_SEQ = OFF_DEV_BIND + 16
DB_REJECT = OFF_DEV_BIND + 20
DB_PERIOD = OFF_DEV_BIND + 24
DB_OK_N = OFF_DEV_BIND + 28
DB_ENTRIES = OFF_DEV_BIND + 32
DB_ERR_N = OFF_DEV_BIND + 64
DB_LAST_ERR = OFF_DEV_BIND + 68
DB_SKIP_N = OFF_DEV_BIND + 72
DB_REJ_N = OFF_DEV_BIND + 76
DB_LOAD_OK_N = OFF_DEV_BIND + 80
DB_LOAD_BAD_N = OFF_DEV_BIND + 84
OFF_DEV_BIND_SZ = 0x60
DB_SLOTS = 8
DB_MAGIC_VAL = 0x444E4244
DB_PERIOD_DEF = 10
DB_PERIOD_MAX = 1000
DB_SEG_LEN = 48
DB_RC_OK = 0
DB_RC_CRC = 1
DB_RC_NAME = {0: "OK", 1: "CRC", 2: "SEQ", 3: "DEV", 4: "ADDR",
              5: "LEN", 6: "DST", 8: "NOSEQ"}
# 工具里各字段在 24 字快照里的下标（★ 由上面的偏移算出, 不手抄 —— 手抄就是"一处改一处忘"）
_W = (OFF_DEV_BIND_SZ // 4)


def _idx(off):
    return (off - OFF_DEV_BIND) // 4


IDX_MAGIC, IDX_NVALID, IDX_CRC, IDX_REQ, IDX_DONE = (
    _idx(DB_MAGIC), _idx(DB_N_VALID), _idx(DB_CRC), _idx(DB_REQ_SEQ), _idx(DB_DONE_SEQ))
IDX_REJECT, IDX_PERIOD, IDX_OKN, IDX_ENT0 = (
    _idx(DB_REJECT), _idx(DB_PERIOD), _idx(DB_OK_N), _idx(DB_ENTRIES))
IDX_ERRN, IDX_LASTERR, IDX_SKIPN, IDX_REJN = (
    _idx(DB_ERR_N), _idx(DB_LAST_ERR), _idx(DB_SKIP_N), _idx(DB_REJ_N))
IDX_LOK, IDX_LBAD = _idx(DB_LOAD_OK_N), _idx(DB_LOAD_BAD_N)

# PROG_RC_*（src/prog_store.h）—— 只用于把 rc 打印成人话
RC_NAME = {0: "OK", 1: "NOPART(卡未分区)", 2: "LEN(长度非法)", 3: "WRITE(卡写失败)",
           4: "VERIFY(回读不符/闸3)", 5: "NONE(无有效副本)", 6: "CRC(CRC不符)",
           7: "VALIDATE(静态校验拒/闸5)", 8: "MINF(固件版本过低)", 9: "CAPS(缺能力位)"}


# ══════════════════ 记账 ══════════════════
_N_PASS = _N_FAIL = _N_SKIP = 0


def record(name, ok, detail=""):
    global _N_PASS, _N_FAIL
    if ok is None:
        globals()["_N_SKIP"] += 1
        print("  [SKIP] %s%s" % (name, ("  " + detail) if detail else ""))
        return None
    if ok:
        _N_PASS += 1
    else:
        _N_FAIL += 1
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    return ok


def skip(name, detail=""):
    global _N_SKIP
    _N_SKIP += 1
    print("  [SKIP] %s%s" % (name, ("  " + detail) if detail else ""))


def hdr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ══════════════════ C 头文件解析（★ 能力位不许写死）══════════════════
def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _defines(text):
    """`#define NAME expr` → dict。先拼掉行继续符 —— 多行宏（如 *_IMPL 的 `|` 组合）必须能解析。"""
    text = re.sub(r"\\\n", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    out = {}
    for m in re.finditer(r"#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+([^\n]*)", text):
        out[m.group(1)] = m.group(2).strip()
    return out


def _eval_expr(expr, defs, depth=0):
    """极小的 C 常量表达式求值器：字面量 / 已定义宏 / `+ - * | ( )`。
    ★ 顺序不能反: 先**遮掉十六进制字面量**再替换标识符 —— 否则 `0x444E4244` 里的 `x444E4244`
      会被当成标识符（实测会把 `DB_MAGIC_VAL` 解析炸掉）。"""
    if depth > 16:
        raise ValueError("宏解析过深(自引用?)")
    s = re.sub(r"(?<=[0-9A-Fa-f])(?:[uUlL]+)\b", "", expr)      # 48u → 48
    hexes = []

    def _mask(m):
        hexes.append(int(m.group(0), 16))
        return " %d " % (len(hexes) - 1)                        # 占位: 纯十进制, 无字母

    s = re.sub(r"\b0[xX][0-9A-Fa-f]+\b", _mask, s)
    s = re.sub(r"~0U|~0u", "4294967295", s)

    def _sub(m):
        n = m.group(0)
        if n in defs:
            return str(_eval_expr(defs[n], defs, depth + 1))
        raise ValueError("未知标识符 %s" % n)

    s = re.sub(r"[A-Za-z_][A-Za-z0-9_]*", _sub, s)
    for i, v in enumerate(hexes):
        s = re.sub(r"\b%d\b" % i, str(v), s)
    if not re.match(r"^[\s0-9+\-*|()&<>,]*$", s):
        raise ValueError("表达式含不支持的东西: %r" % s)
    return int(eval(s, {"__builtins__": {}}, {}))               # 仅仓库内头文件, 无外部输入


def load_header_facts():
    """→ (defs, err)。err 非空 = 解析失败（前置不成立，不当成 PASS）。"""
    try:
        d = {}
        d.update(_defines(_read(os.path.join(SRC, "transport.h"))))
        d.update(_defines(_read(os.path.join(SRC, "engine.h"))))
        d.update(_defines(_read(os.path.join(SRC, "dev_bind.h"))))
        d.update(_defines(_read(os.path.join(SRC, "prog_store.h"))))
        return d, None
    except Exception as e:
        return None, "%s" % e


def cap_bit_from_header(defs, override=None):
    """★ 能力位**不写死**: 从 src/transport.h 的宏解析（`--cap-bit` 可覆盖）。
    → (bit, note)"""
    if override is not None:
        return override, "--cap-bit 给定 (=0x%X)" % override
    try:
        bit = _eval_expr(defs["DCL_CAP_DEVBIND_PERSIST"], defs)
    except Exception as e:
        return None, "src/transport.h 里没有可解析的 DCL_CAP_DEVBIND_PERSIST (%s)" % e
    note = "src/transport.h: DCL_CAP_DEVBIND_PERSIST = 0x%X" % bit
    try:
        impl = _eval_expr(defs["DCL_CAP_H723_IMPL"], defs)
        note += "；DCL_CAP_H723_IMPL = 0x%X" % impl
        if not (impl & bit):
            note += "  ← ★ 该位**不在** IMPL 宏里（固件不会宣称它）"
    except Exception:
        pass
    return bit, note


# ══════════════════ 纯逻辑件（离线可自检）══════════════════
def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def db_crc_words(entries):
    """`src/dev_bind.c: db_crc()` 的逐字同构实现（**按 32 位字**的 FNV-1a）。"""
    h = 2166136261
    for w in entries:
        h = ((h ^ (w & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h


def entry(dev, dst, ln, reg, addr7):
    """契约 §3.8.2 的条目编码: [31:28]dev [27:24]dst [23:16]len [15:8]reg [7:0]addr7"""
    return (((dev & 0xF) << 28) | ((dst & 0xF) << 24) | ((ln & 0xFF) << 16)
            | ((reg & 0xFF) << 8) | (addr7 & 0xFF))


def entry_note(e):
    dev = (e >> 28) & 0xF
    if dev == 0:
        return "*空槽*"
    return "dev=%d dst=%d len=%d reg=0x%02X addr7=0x%02X" % (
        dev, (e >> 24) & 0xF, (e >> 16) & 0xFF, (e >> 8) & 0xFF, e & 0xFF)


def build_segment(period, entries, fnv=None):
    """构造 48 B 段。`fnv=None` ⇒ 按 entries 正确算出；给出 ⇒ 用给定值（T4 造坏段用）。"""
    e = (list(entries) + [0] * DB_SLOTS)[:DB_SLOTS]
    seg = bytearray(DB_SEG_LEN)
    struct.pack_into("<I", seg, 0, DB_MAGIC_VAL)
    struct.pack_into("<I", seg, 4, DB_SEG_LEN)
    struct.pack_into("<I", seg, 8, period & 0xFFFFFFFF)
    struct.pack_into("<I", seg, 12, db_crc_words(e) if fnv is None else (fnv & 0xFFFFFFFF))
    for i, v in enumerate(e):
        struct.pack_into("<I", seg, 16 + 4 * i, v & 0xFFFFFFFF)
    return bytes(seg)


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def seg_probe(payload):
    """**镜像** `dev_bind_unpack()` 的判定（离线自检用）→ ('none'|'bad'|'ok', seg_bytes)

    ★ 这不是"第二份实现"，而是把固件的判据用 Python 重述一遍，好在**不接板子**时
      就能证明 T1/T3/T4 用的载荷确实分别落在"有段/无段/段坏"三个不同分支上。
    """
    n = len(payload)
    if n < DB_SEG_LEN:
        return "none", None
    seg = payload[n - DB_SEG_LEN:]
    if u32(seg, 0) != DB_MAGIC_VAL:
        return "none", None
    if u32(seg, 4) != DB_SEG_LEN:
        return "bad", seg
    ents = [u32(seg, 16 + 4 * i) for i in range(DB_SLOTS)]
    if db_crc_words(ents) != u32(seg, 12):
        return "bad", seg
    return "ok", seg


def build_prog(n_routes=2, n_params=1):
    """合法程序载荷体 `[nr][np][ns] + routes + params + states`（**不带段**）。

    ★ 字段偏移**逐字节抄 `src/engine.h` 的 `RouteEntry_t`**（本项目踩过：照文档表格估偏移
      会把 `flags` 写到 byte 14 而真值在 **byte 5** ⇒ 全 0 条 ACTIVE ⇒ 白查一轮）。
    真布局 (packed 16B): [0]src_type [1]src_index [2]dst_type [3]dst_channel
                         [4]op [5]flags [6:8]param_idx [8:10]state_offset
                         [10:12]actuator_idx [12:14]wire2_idx [14]period [15]reserved
    """
    hdr3 = struct.pack("<HHH", n_routes, n_params, 0)
    routes = b""
    for i in range(n_routes):
        r = bytearray(16)
        r[0] = 2        # SRC_CONST
        r[2] = 2        # DST_WIRE
        r[3] = 8 + i    # WIRE[8+i]
        r[4] = 0x00     # OP_DIRECT
        r[5] = 0x01     # ROUTE_FLAG_ACTIVE ← ★ byte 5
        routes += bytes(r)
    params = struct.pack("<ffff", 1.0, 0.0, 0.0, 0.0) * n_params
    body = hdr3 + routes + params
    assert len(body) == 6 + (n_routes + n_params) * 16
    return body


def program_body_len(n_routes=2, n_params=1):
    """`prog_validate` 眼里"这份程序本身有多长" = 6 + (nr+np+ns)*16"""
    return 6 + (n_routes + n_params) * 16


def chunk_plan(total, chunk=140):
    """分片计划 → [(off, bytes)]；★ 供离线自检核对"分片恰好覆盖 [0,total) 一次、序号连续"。"""
    out = []
    off = 0
    while off < total:
        c = min(chunk, total - off)
        out.append((off, c))
        off += c
    return out


# ══════════════════ 板上通道 ══════════════════
_CLIENT = None


def client_mod():
    global _CLIENT
    if _CLIENT is None:
        import h723_client
        _CLIENT = h723_client
    return _CLIENT


class Board:
    def __init__(self, port, timeout=2.5, verbose=False):
        Dcl = client_mod().Dcl
        self.d = Dcl(port, wait=0.6, timeout=timeout)
        self.verbose = verbose
        self.shm = None

    def send(self, cmd, payload=b"", expect_len=None):
        sts, p = self.d.send(cmd, payload, expect_len=expect_len)
        if self.verbose:
            print("      %02X -> %s %s" % (cmd, sts, bytes(p)[:32].hex()))
        return sts, bytes(p)

    def rd(self, off, count):
        """0x22 读 count 个 u32（off = SHM 偏移）"""
        assert 0 < count <= 256
        sts, p = self.send(C_READ_BURST, struct.pack("<IH", self.shm + off, count),
                           expect_len=count * 4)
        if sts != "ACK" or len(p) < count * 4:
            return None
        return list(struct.unpack("<%dI" % count, p[:count * 4]))

    def wr(self, off, words):
        """0x23 写 n 个 u32"""
        assert 0 < len(words) <= 256
        sts, p = self.send(C_WRITE_BURST,
                           struct.pack("<IH", self.shm + off, len(words))
                           + struct.pack("<%dI" % len(words), *words), expect_len=4)
        return sts == "ACK"

    def wr1(self, off, val):
        """0x21 写 1 个 u32"""
        sts, p = self.send(C_WRITE, struct.pack("<II", self.shm + off, val), expect_len=4)
        return sts == "ACK"

    # ── 程序事务（0x45/0x46/0x47）──
    def prog_begin(self, total, crc, prog_id=0xDB0001, prog_ver=1, min_fw=0, req_caps=0):
        pl = struct.pack("<II I H H I", total, crc, prog_id, prog_ver, min_fw, req_caps)
        return self.send(C_PROG_BEGIN, pl, expect_len=4)

    def prog_data(self, off, chunk):
        return self.send(C_PROG_DATA, struct.pack("<I", off) + chunk, expect_len=4)

    def upload(self, payload, chunk=140):
        """事务式上传。→ (ok, log, detail)"""
        log = []
        sts, _ = self.prog_begin(len(payload), crc32(payload))
        log.append(("BEGIN", sts))
        if sts != "ACK":
            return False, log, "BEGIN 未被受理 (%s)。若固件是旧版, 0x45 可能不存在。" % sts
        for off, n in chunk_plan(len(payload), chunk):
            sts, _ = self.prog_data(off, payload[off:off + n])
            log.append(("DATA@%d" % off, sts))
            if sts != "ACK":
                return False, log, "DATA@%d 未被受理 (%s)" % (off, sts)
        sts, p = self.send(C_PROG_COMMIT, b"", expect_len=8)
        log.append(("COMMIT", sts))
        if sts != "ACK":
            return False, log, "COMMIT 被拒 (%s)" % sts
        rc = u32(p, 0)
        if rc != 0:
            return False, log, "COMMIT 返回 rc=%d(%s)" % (rc, RC_NAME.get(rc, "?"))
        return True, log, "COMMIT ok (budget=%d)" % u32(p, 4)

    def prog_status(self):
        """0x48 → dict（**基础 96 B = 24 字**；2026-09-16 起尾部追加到 104 B）

        ★★★ 长度判据必须写 **`>= 96`**，不能写 `== 96` —— 否则一次**只追加不改前面**
        的向后兼容扩展就会把工具打红。这与本项目 `cap == 0x0DF7` 那个陷阱**同族**：
        **等值判据挡不住"合法地变长"**。新增字段（+96 boot_seg / +100 seg_len）在有 104 B
        时才解析，老固件（96 B）则为 None —— 两种都能读。"""
        sts, p = self.send(C_PROG_STATUS, b"", expect_len=None)
        if sts != "ACK" or len(p) < 96:
            return None
        w = struct.unpack("<24I", p[:96])
        tail = {}
        if len(p) >= 104:
            t = struct.unpack("<26I", p[:104])
            tail = {"boot_seg": t[24], "seg_len": t[25]}   # ★ GAP-11 尾部扩展（0=无段/1=恢复/2=段坏; 段长 0 或 48）
        return dict(part_ok=w[0], ab_valid=w[1], seq_a=w[2], seq_b=w[3],
                    crc_a=w[4], crc_b=w[5], len_a=w[6], len_b=w[7], active=w[8],
                    ok_n=w[9], fail_n=w[10], reject_n=w[11], last_rc=w[12],
                    txn_begin=w[13], txn_commit=w[14], txn_abort=w[15],
                    boot_rc=w[16], boot_loaded=w[17], deploy_routes=w[18], deploy_seq=w[19],
                    pl0=w[20], pl1=w[21], pl2=w[22], pl3=w[23],
                    **tail)   # ★ GAP-11 尾部扩展（老固件 96B ⇒ tail 为空, 键不存在）

    def stored_len(self, st):
        """当前生效副本的**实际存储载荷长度**（= 落盘时 len, 含可选段）。None = 无有效副本。"""
        if not st:
            return None
        if st["active"] == 0:
            return st["len_a"]
        if st["active"] == 1:
            return st["len_b"]
        return None

    def snap(self):
        """一次 0x22 读回整个提交区（24 字）—— 保证各字段**同一次采样**。"""
        w = self.rd(OFF_DEV_BIND, _W)
        if w is None or len(w) < _W:
            return None
        return dict(magic=w[IDX_MAGIC], n_valid=w[IDX_NVALID], crc=w[IDX_CRC],
                    req_seq=w[IDX_REQ], done_seq=w[IDX_DONE], reject=w[IDX_REJECT],
                    period=w[IDX_PERIOD], ok_n=w[IDX_OKN],
                    entries=list(w[IDX_ENT0:IDX_ENT0 + DB_SLOTS]),
                    err_n=w[IDX_ERRN], last_err=w[IDX_LASTERR], skip_n=w[IDX_SKIPN],
                    rej_n=w[IDX_REJN], load_ok=w[IDX_LOK], load_bad=w[IDX_LBAD])

    def reload(self):
        """0x39 op=23 = 重新执行开机装载（与开机**同一条代码路径**，见 §0）。

        ★★ 这里**故意不用** `expect_len`。理由不是省事, 而是那条路会**重发**:
          `Dcl.send(expect_len=...)` 长度不符时最多重发 3 次 —— 而 op=23 **有副作用**
          （它会再装载一次，`DB_LOAD_OK_N` 会 +1）。重发会让 T1.5/T5.6 的计数判据
          变成"数了 3 次还是 1 次"的谜题。⇒ 单发一次, 长度/内容由本函数**自己**校验:
          长度必须 8、`rc` 必须是已知的 `PROG_RC_*`（0..9）—— 这已经拦掉绝大多数杂帧。
        → (rc, loaded) / (None, None)；详细错误放 self.last_err
        """
        self.last_err = ""
        sts, p = self.send(C_PIN_PATTERN, bytes([OP_RELOAD, 0]))
        if sts != "ACK":
            self.last_err = "op=23 应答 %s（op=23 未实现? 固件未烧?）" % sts
            return None, None
        if len(p) != 8:
            self.last_err = "op=23 应答长度 %d != 8（疑似串帧）" % len(p)
            return None, None
        rc, loaded = u32(p, 0), u32(p, 4)
        if rc > 9:
            self.last_err = "op=23 的 rc=%d 不是已知的 PROG_RC_*（疑似串帧）" % rc
            return None, None
        return rc, loaded

    def close(self):
        self.d.close()


# ══════════════════ 等待（**每条都有上界**）══════════════════
def wait_done(b, seq, timeout=2.0):
    """等 `done_seq == seq`（表生效）。→ (ok, snap)"""
    end = time.time() + timeout
    s = None
    while True:
        s = b.snap()
        if s is not None and s["done_seq"] == seq:
            return True, s
        if time.time() >= end:
            return False, s
        time.sleep(0.01)


def wait_settle(b, timeout=2.0):
    """等 `req_seq == done_seq`（提交被收尾/生效）。→ (ok, snap)"""
    end = time.time() + timeout
    s = None
    while True:
        s = b.snap()
        if s is not None and s["req_seq"] == s["done_seq"]:
            return True, s
        if time.time() >= end:
            return False, s
        time.sleep(0.01)


def wait_growth(b, keys, base, timeout=3.0, step=0.03):
    """等到 base 里某个键增长。→ (命中的键, snap)"""
    end = time.time() + timeout
    s = None
    while time.time() < end:
        s = b.snap()
        if s is not None:
            for k in keys:
                if s[k] > base[k]:
                    return k, s
        time.sleep(step)
    return None, (s if s is not None else b.snap())


# ══════════════════ 表：提交 / 比较 ══════════════════
_seq = 0


def submit(b, scan_entries, period, seq=None):
    """把表写进 SHM 并提交。写序: entries → crc → period → **req_seq 最后**。

    为什么这个顺序（照 `h723_dev_bind_test.py` 的既有理由）: 固件的受理条件是
    `req_seq != done_seq`；先写 req_seq 就可能让固件在 entries 只写了一半时受理
    —— 而"半个表"正是 CRC 要防的东西。seq 最后写 ⇒ 固件看到它时表与 crc 一定已就位。

    ★★★ 序号**必须从设备当前的 req_seq 往上取**, 不能只用一个本地计数器。
      理由（本脚本的反空自检 `h723_devbind_persist_sim.py` 抓出来的真缺陷）:
      `dev_bind_unpack()` 每次恢复都会 `DB_REQ_SEQ += 1`（src/dev_bind.c）。于是
      重装载之后设备的 `req_seq` 可能**恰好等于或超过**本地计数器 —— 此时我们写下的
      `req_seq` 与 `done_seq` 相同 ⇒ 固件判"已处理"**直接 return** ⇒ **表压根没提交**,
      而工具的 `wait_done()` 立刻看到 `done_seq == seq` 就当成成功, 读到的是**旧表**。
      症状: T3 的"清空活表"静默失败, 之后每条判据都在拿旧表判 —— 而**没有任何错误迹象**。
      ⇒ 起点 = `max(本地计数, 设备当前 req_seq) + 1`（顺带也躲开 DB_RC_SEQ 回退拒绝）。
    → (seq, snap)；snap 为 None = 读不到或**序号被人动过**（本判据不可用, 不当成成功）
    """
    global _seq
    ents = (list(scan_entries) + [0] * DB_SLOTS)[:DB_SLOTS]
    crc = db_crc_words(ents)
    w = b.rd(DB_REQ_SEQ, 2)                 # [req_seq, done_seq]
    dev_req = w[0] if w else 0
    seq = max(_seq, dev_req) + 1
    _seq = seq
    if not b.wr(DB_ENTRIES, ents):
        return seq, None
    if not b.wr1(DB_CRC, crc):
        return seq, None
    if not b.wr1(DB_PERIOD, period):
        return seq, None
    if not b.wr1(DB_REQ_SEQ, seq):
        return seq, None
    ok, s = wait_done(b, seq, timeout=2.0)
    if s is not None and s["req_seq"] != seq and s["done_seq"] == seq:
        return seq, None                    # 序号对不上 ⇒ 不采信这张快照
    return seq, s


def same_table(a, b_):
    """逐字段比两张表（n_valid / period / entries[8]）"""
    if a is None or b_ is None:
        return False, "有一侧读不到"
    for k in ("n_valid", "period"):
        if a[k] != b_[k]:
            return False, "%s %s → %s" % (k, a[k], b_[k])
    if a["entries"] != b_["entries"]:
        return False, "entries 变了:\n         前 %s\n         后 %s" % (
            [hex(x) for x in a["entries"]], [hex(x) for x in b_["entries"]])
    return True, "n_valid=%d period=%d entries=%s" % (
        a["n_valid"], a["period"], " ".join("%08X" % x for x in a["entries"]))


def tbl_str(s):
    if s is None:
        return "(读不到)"
    return ("magic=0x%08X n_valid=%d req=%d done=%d reject=%d(%s) period=%d "
            "ok_n=%d err_n=%d last_err=%d(%s) skip_n=%d rej_n=%d load_ok=%d load_bad=%d"
            " entries=[%s]" % (
                s["magic"], s["n_valid"], s["req_seq"], s["done_seq"], s["reject"],
                DB_RC_NAME.get(s["reject"], "?"), s["period"], s["ok_n"], s["err_n"],
                s["last_err"], I2C_SM_ST_NAME.get(s["last_err"], "?"), s["skip_n"],
                s["rej_n"], s["load_ok"], s["load_bad"],
                " ".join("%08X" % x for x in s["entries"])))


# ══════════════════ T0 静态对账（离线可跑）══════════════════
_TOOL_CONSTS = [
    ("OFF_DEV_BIND", OFF_DEV_BIND), ("DB_MAGIC", DB_MAGIC), ("DB_N_VALID", DB_N_VALID),
    ("DB_CRC", DB_CRC), ("DB_REQ_SEQ", DB_REQ_SEQ), ("DB_DONE_SEQ", DB_DONE_SEQ),
    ("DB_REJECT", DB_REJECT), ("DB_PERIOD", DB_PERIOD), ("DB_OK_N", DB_OK_N),
    ("DB_ENTRIES", DB_ENTRIES), ("DB_ERR_N", DB_ERR_N), ("DB_LAST_ERR", DB_LAST_ERR),
    ("DB_SKIP_N", DB_SKIP_N), ("DB_REJ_N", DB_REJ_N), ("DB_LOAD_OK_N", DB_LOAD_OK_N),
    ("DB_LOAD_BAD_N", DB_LOAD_BAD_N), ("OFF_DEV_BIND_SZ", OFF_DEV_BIND_SZ),
    ("DB_SLOTS", DB_SLOTS), ("DB_MAGIC_VAL", DB_MAGIC_VAL),
    ("DB_PERIOD_DEF", DB_PERIOD_DEF), ("DB_PERIOD_MAX", DB_PERIOD_MAX),
    ("DB_SEG_LEN", DB_SEG_LEN),
    ("DB_RC_OK", DB_RC_OK), ("DB_RC_CRC", DB_RC_CRC),
]


def static_mismatches(defs, consts=None):
    """把工具里的常量与 `src/*.h` 逐个对账 → 不一致项列表（空 = 一致）。

    ★ 为什么必须有这条: SHM 偏移是本工具**唯一的立足点** —— 偏一个字节, 后面每个读数
      都会变成"看起来合理但全错"（本项目"一个语义两处存放"那一族的标准形态）。
      这条判据的红条件 = 任一处不等 ⇒ **前置不成立**, 退出 1（不是 PASS）。
    ★ `consts` 可注入（离线自检用**故意改错的**表来证明这条判据真的会红, 不是恒真）。
    """
    bad = []
    for name, mine in (consts if consts is not None else _TOOL_CONSTS):
        try:
            theirs = _eval_expr(defs[name], defs)
        except Exception as e:
            bad.append("%s: 源码里解析不了 (%s)" % (name, e))
            continue
        if theirs != mine:
            bad.append("%s: 工具=%d(0x%X) 源码=%d(0x%X)" % (name, mine, mine, theirs, theirs))
    # 表内每个字段的下标必须落在 %d 字快照里（越界 = 读到的其实是别人的数）
    for name in ("DB_LAST_ERR", "DB_SKIP_N", "DB_REJ_N", "DB_LOAD_OK_N", "DB_LOAD_BAD_N"):
        try:
            if (_eval_expr(defs[name], defs) - OFF_DEV_BIND) // 4 >= _W:
                bad.append("%s 的下标越出 %d 字快照 (%d 字)" % (name, _W, OFF_DEV_BIND_SZ // 4))
        except Exception as e:
            bad.append("%s 下标算不出来 (%s)" % (name, e))
    return bad


def t0_static(defs, consts=None, label=""):
    bad = static_mismatches(defs, consts)
    if bad:
        record("T0.3%s 工具常量 == src/*.h（偏移/段长/拒绝码逐项对账）" % label, False,
               "共 %d 处不一致:\n         %s" % (len(bad), "\n         ".join(bad)))
        return False
    record("T0.3%s 工具常量 == src/*.h（%d 项逐项对账）" % (label, len(_TOOL_CONSTS)), True,
           "OFF_DEV_BIND=0x%X SZ=0x%X SLOTS=%d SEG_LEN=%d" % (
               OFF_DEV_BIND, OFF_DEV_BIND_SZ, DB_SLOTS, DB_SEG_LEN))
    return True


# ══════════════════ T0 前置 ══════════════════
def t0_preflight(b, cap_bit):
    hdr("T0 前置: 链路 / 能力位 / 区自证 / 可装载")
    sts, p = b.send(C_VERSION, b"", expect_len=4)
    if sts != "ACK" or len(p) != 4:
        record("T0.1 链路活（0x01 → ACK 4B）", False, "sts=%s len=%d" % (sts, len(p)))
        return False
    fw = p[0] | (p[1] << 8)
    cap = p[2] | (p[3] << 8)
    print("  设备: fw=0x%04X  cap=0x%04X" % (fw, cap))
    record("T0.1 链路活（0x01 → ACK 4B, CRC 通过）", True, "fw=0x%04X cap=0x%04X" % (fw, cap))

    if cap_bit is None:
        record("T0.2 能力字含 DCL_CAP_DEVBIND_PERSIST", False,
               "无法确定该位: src/transport.h 未定义该宏 且 未给 --cap-bit ⇒ 前置不成立")
        return False
    if not (cap & cap_bit):
        record("T0.2 能力字含 DCL_CAP_DEVBIND_PERSIST(0x%X)" % cap_bit, False,
               "cap=0x%04X **不含**该位 ⇒ 这是**老固件/未声明**的固件, 本验收**前置不成立** "
               "（明确不判 PASS）" % cap)
        return False
    record("T0.2 能力字含 DCL_CAP_DEVBIND_PERSIST(0x%X)" % cap_bit, True,
           "cap=0x%04X ✓ (掩码判据, 不是等值 —— 见 h723_client.link_alive 的说明)" % cap)

    sts, p = b.send(C_ENGINE_STATUS, b"", expect_len=ENG_STATUS_LEN)
    if sts != "ACK" or len(p) < 27:
        record("T0.4 0x38 可读（用于发现 SHM 基址）", False, "sts=%s len=%d" % (sts, len(p)))
        return False
    b.shm = u32(p, 23)
    if b.shm == 0:
        record("T0.4 0x38 的 shm 基址非 0", False, "shm=0")
        return False
    print("  SHM 基址 = 0x%08X" % b.shm)

    s = b.snap()
    if s is None or s["magic"] != DB_MAGIC_VAL:
        record("T0.5 ★ SHM 区自证 DB_MAGIC == 'DBND'", False,
               "读回 %s ⇒ 偏移可能不对, 此时后续每个读数都不可信" % tbl_str(s))
        return False
    record("T0.5 ★ SHM 区自证 DB_MAGIC == 'DBND'", True, tbl_str(s))

    st = b.prog_status()
    if st is None:
        record("T0.6 0x48 PROG_STATUS 可读（96B）", False, "无应答/长度不符")
        return False
    if st["part_ok"] != 1:
        record("T0.7 SD 已分区（part_ok==1 ⇒ 程序存储可用）", False,
               "part_ok=%d ⇒ 程序存储不可用, 本验收不成立" % st["part_ok"])
        return False
    record("T0.7 SD 已分区（part_ok==1）", True,
           "ab_valid=%d active=%s len_a=%d len_b=%d txn begin/commit/abort=%d/%d/%d" % (
               st["ab_valid"], st["active"], st["len_a"], st["len_b"],
               st["txn_begin"], st["txn_commit"], st["txn_abort"]))
    return True


# ══════════════════ T1 ══════════════════
def t1_restore(b):
    """上传（固件附加段）→ 换掉活表 → op=23 → 表必须**从段里**恢复回来。

    顺序（★ 中间那一步是反空判据的关键）:
      ① 提交表 A 并等生效（固件此刻才"有表可打包"）
      ② 上传裸程序 ⇒ 固件在 COMMIT 时把表 A 打成 48 B 段塞进载荷尾部
      ③ **把活表换成 B（空表 + 别的 period）** —— 否则"不实现恢复"也会 PASS
      ④ op=23 ⇒ 表必须变回 A（n_valid/period/entries 逐字段）
    → (ok, post_snap)
    """
    hdr("T1 上传含段的包 → op=23 重装载 → 绑定表**从段里**恢复")
    base_len = len(PROG_BODY)

    seq, sA = submit(b, TABLE_A, PERIOD_A)
    if sA is None or sA["n_valid"] != len(TABLE_A) or sA["reject"] != DB_RC_OK:
        record("T1.0 表 A 提交并被接受", False,
               "期望 n_valid=%d reject=0；实际 %s" % (len(TABLE_A), tbl_str(sA)))
        return False, None
    record("T1.0 表 A 提交并被接受（`dev_bind_pack` 的前提: s_n != 0）", True, tbl_str(sA))

    ok, log, det = b.upload(PROG_BODY)
    if not ok:
        record("T1.0 上传裸程序（0x45/0x46/0x47）", False, "%s；log=%s" % (det, log))
        return False, None
    st = b.prog_status()
    slen = b.stored_len(st)
    record("T1.1 ★ 落盘长度 == 裸长 %d + 48（固件确实附加了段）" % base_len,
           slen == base_len + DB_SEG_LEN,
           "stored_len=%s（期望 %d）%s" % (
               slen, base_len + DB_SEG_LEN,
               "" if slen == base_len + DB_SEG_LEN else
               "  ← 长度不对: 若 ==%d 说明**没附加**（设备侧有活表却打成空段）" % base_len))
    if slen != base_len + DB_SEG_LEN:
        print("        ※ 段没被附加 ⇒ T1 的其余判据失去对象（仍继续跑, 让症状自己说话）")

    # ③ 反空判据: 把活表换成另一张, 且 period 也不同
    seqB, sB = submit(b, [], PERIOD_B)
    record("T1.2 反空判据: 活表已被换成空表(period=%d) —— 恢复必须来自段, 不能来自「现状」"
           % PERIOD_B, sB is not None and sB["n_valid"] == 0 and sB["period"] == PERIOD_B,
           tbl_str(sB))
    if sB is None:
        return False, None

    pre = b.snap()
    pre_st = b.prog_status()
    rc, loaded = b.reload()
    if rc is None:
        record("T1.3 op=23 受理（0x39 op=23 → ACK 8B）", False, b.last_err)
        return False, None
    settled, post = wait_settle(b, 2.0)     # ★ 恢复可能被延后(有事务在飞⇒s_pend), 必须等生效
    post_st = b.prog_status()
    record("T1.3 op=23 受理且 rc==PROG_RC_OK", rc == DB_RC_OK,
           "rc=%d(%s) loaded=%d；stored_len=%s；settle=%s" % (
               rc, RC_NAME.get(rc, "?"), loaded, b.stored_len(post_st), settled))
    if rc != DB_RC_OK:
        return False, None
    if not settled:
        print("        ※ 2.0s 内 req_seq 没被 done_seq 追上 ⇒ 下面按「未恢复」判")

    eq, det = same_table(sA, post)
    record("T1.4 ★ 表**从段里**恢复（n_valid/period/entries 逐字段 == 上传前）", eq,
           det if eq else "%s  ← 固件没实现恢复 ⇒ 读回的是空表" % det)
    d_ok = post["load_ok"] - pre["load_ok"]
    record("T1.5 DB_LOAD_OK_N +1（恢复成功被记数 ⇒ 真走了恢复路径）", d_ok == 1,
           "load_ok %d→%d (Δ=%d)  load_bad %d→%d  deploy_seq %s→%s" % (
               pre["load_ok"], post["load_ok"], d_ok, pre["load_bad"], post["load_bad"],
               pre_st["deploy_seq"] if pre_st else "?",
               post_st["deploy_seq"] if post_st else "?"))
    return eq, post


# ══════════════════ T2 ══════════════════
def t2_polling(b, pre):
    """恢复之后表必须**真的在轮询**（不是只把字段填回去）。

    ★ 不假定板子接了 AS5600（本项目先例: `h723_dev_bind_test.py` 的 T5.2/T5.3）:
      接了 ⇒ `ok_n` 涨; 没接 ⇒ 只有 `err_n` 涨 且 `last_err==NAK(3)` —— **那也是 PASS**。
    → (在场?, snap)
    """
    hdr("T2 恢复的表真的在轮询（ok_n 或 err_n 至少一个在涨）")
    if pre is None:
        skip("T2.1 计数器在动", "T1 已有 FAIL ⇒ 无法判定（不当作 PASS）")
        skip("T2.2 器件在场分类", "同上")
        return None, None
    base = dict(ok_n=pre["ok_n"], err_n=pre["err_n"])
    key, s = wait_growth(b, ("ok_n", "err_n"), base, timeout=3.0)
    record("T2.1 ★ 计数器在动（区分「表恢复了但 service 没跑」）", key is not None,
           "n_valid=%s  ok_n %d→%s  err_n %d→%s  skip_n=%s  last_err=%s" % (
               pre["n_valid"],
               base["ok_n"], s["ok_n"] if s else "?",
               base["err_n"], s["err_n"] if s else "?",
               s["skip_n"] if s else "?",
               I2C_SM_ST_NAME.get(s["last_err"], "?") if s else "?"))
    if key is None:
        print("        !! 表恢复了(done_seq 跟上)但 ok_n/err_n 都不动 ⇒ 最可能:")
        print("           `dev_bind_service()` 没在主循环被调用, 或恢复走的不是真表。")
        print("           （不是接线问题 —— 接线问题会让 err_n 涨。）")
        return False, s
    if key == "ok_n":
        print("        ⇒ **器件在场**: ok_n 在涨（I2C 真的读到了 AS5600）")
        record("T2.2 器件在场判定: ok_n 在涨 ⇒ 接有 AS5600", True,
               "ok_n %d→%d" % (base["ok_n"], s["ok_n"]))
        return True, s
    present = s["last_err"] == I2C_SM_ST_NAK
    print("        ⇒ **未接器件**: 只有 err_n 在涨, last_err=%d(%s)" % (
        s["last_err"], I2C_SM_ST_NAME.get(s["last_err"], "?")))
    print("           ★ 判 PASS, 但**未接器件** —— 不要把这个结果读成「读到 0 度」。")
    record("T2.2 未接器件判定: err_n 在涨 且 last_err==NAK(3)", present,
           "err_n %d→%d last_err=%d(%s)%s" % (
               base["err_n"], s["err_n"], s["last_err"],
               I2C_SM_ST_NAME.get(s["last_err"], "?"),
               "" if present else "  ← err 在涨但**不是 NAK** ⇒ 这是器件/接线问题,"
                                  " 不是持久化缺陷（先排除硬件再谈本判据）"))
    return False, s


# ══════════════════ T3 ══════════════════
def t3_no_segment(b):
    """老包（无段）: 长度必须恰好是载荷体长, 且重装载**不恢复**任何表。"""
    hdr("T3 老格式包（无段）→ 长度不变 + 重装载不恢复")
    base_len = len(PROG_BODY)

    seq, s0 = submit(b, [], PERIOD_B)
    record("T3.0 先清空活表（s_n==0 ⇒ 固件不该附加空段）",
           s0 is not None and s0["n_valid"] == 0,
           "%s  ← 无绑定是 `dev_bind_pack` 提前返回的条件" % tbl_str(s0))
    if s0 is None:
        return
    ok, log, det = b.upload(PROG_BODY)
    if not ok:
        record("T3.0 上传裸程序", False, "%s；log=%s" % (det, log))
        return
    st = b.prog_status()
    slen = b.stored_len(st)
    record("T3.1 ★ 落盘长度 == 6+(nr+np+ns)*16 == %d（**没有**凭空多 48 B）" % base_len,
           slen == base_len,
           "stored_len=%s（期望 %d）%s" % (
               slen, base_len,
               "" if slen == base_len else
               "  ← 多出 %s B ⇒ 固件**无条件附加段** ⇒ 老包语义被破坏" % (
                   (slen - base_len) if isinstance(slen, int) else "?")))

    pre = b.snap()
    pre_st = b.prog_status()
    rc, loaded = b.reload()
    if rc is None:
        record("T3.2 op=23 受理", False, b.last_err)
        return
    settled, post = wait_settle(b, 2.0)      # ★ 老包不动 req_seq ⇒ 立刻 settle
    post_st = b.prog_status()
    d_seq = (post_st["deploy_seq"] - pre_st["deploy_seq"]) if (pre_st and post_st) else None
    record("T3.2 op=23 后**程序照常装载**（rc==0 且 deploy_seq 增 ≥1）",
           rc == DB_RC_OK and (d_seq is not None and d_seq >= 1),
           "rc=%d(%s) deploy_seq %s→%s (Δ=%s)" % (
               rc, RC_NAME.get(rc, "?"),
               pre_st["deploy_seq"] if pre_st else "?", post_st["deploy_seq"] if post_st else "?",
               d_seq))
    record("T3.3 ★ 老包 ⇒ n_valid == 0（不恢复）", post["n_valid"] == 0,
           "n_valid=%s  ← 非 0 说明装载读了**较旧**的那一份副本(它带段)" % post["n_valid"])
    dl_ok = post["load_ok"] - pre["load_ok"]
    dl_bad = post["load_bad"] - pre["load_bad"]
    record("T3.4 老包下两个装载计数**都不动**（无段 ⇒ 既不恢复也不拒绝）",
           dl_ok == 0 and dl_bad == 0,
           "load_ok Δ=%d  load_bad Δ=%d%s" % (
               dl_ok, dl_bad,
               "" if (dl_ok == 0 and dl_bad == 0) else
               "  ← 无段却被记成「恢复/拒绝」⇒ 那个计数以后没法回答「到底有没有恢复」"))


# ══════════════════ T4 ══════════════════
def t4_bad_fnv(b):
    """段在但 fnv 对不上 ⇒ **明确拒绝该段**（不半装载）+ 程序照常装载 + 拒绝可观测。

    ★ 构造方式（唯一可行的一种, 见 §1）: 先把活表清空（`s_n==0` ⇒ 固件**不会**附加自己的段），
      再由**上位机**把 48 B 假段（magic/seg_len/条目都合法, **只有 fnv 被改坏**）拼在载荷尾部。
      于是落盘内容 == 我们发的; 装载时尾部那 48 B 就是那个坏段。
      条目取合法值 ⇒ "被拒"只可能来自 fnv 这一条判据（可归因）。
    """
    hdr("T4 段内 fnv 被改坏 → 段被明确拒绝 + 程序照常装载 + 拒绝可观测")
    base_len = len(PROG_BODY)

    seq, s0 = submit(b, [], PERIOD_B)
    if s0 is None or s0["n_valid"] != 0:
        record("T4.0 先清空活表（上位机才能自己决定载荷尾部那 48 B）", False, tbl_str(s0))
        return

    good = build_segment(PERIOD_A, TABLE_A)
    bad = build_segment(PERIOD_A, TABLE_A, fnv=u32(good, 12) ^ 0xDEADBEEF)
    payload = PROG_BODY + bad
    kind, _ = seg_probe(payload)
    print("        自检: 该载荷经 `dev_bind_unpack` 镜像判定 = %s（应为 bad）; "
          "fnv 段内=0x%08X 正确值=0x%08X" % (kind, u32(bad, 12), u32(good, 12)))
    ok, log, det = b.upload(payload)
    if not ok:
        record("T4.0 上传带坏段的包", False, "%s；log=%s" % (det, log))
        return
    st = b.prog_status()
    slen = b.stored_len(st)
    record("T4.0 落盘长度 == 裸长 %d + 48（PC 拼的段被**原样**存下）"
           % base_len, slen == base_len + DB_SEG_LEN,
           "stored_len=%s（期望 %d）%s" % (
               slen, base_len + DB_SEG_LEN,
               "" if slen == base_len + DB_SEG_LEN else "  ← 固件又附加了一段 / 截断了"))

    pre = b.snap()
    pre_st = b.prog_status()
    rc, loaded = b.reload()
    if rc is None:
        record("T4.1 op=23 受理", False, b.last_err)
        return
    settled, post = wait_settle(b, 2.0)      # ★ 坏段不动 req_seq（不半装载）
    post_st = b.prog_status()
    d_seq = (post_st["deploy_seq"] - pre_st["deploy_seq"]) if (pre_st and post_st) else None
    record("T4.1 ★ 段坏但**程序照常装载**（rc==0 且 deploy_seq 增 ≥1）",
           rc == DB_RC_OK and (d_seq is not None and d_seq >= 1),
           "rc=%d(%s) deploy_seq Δ=%s（绑定表不是程序正确性的必要条件）" % (
               rc, RC_NAME.get(rc, "?"), d_seq))
    record("T4.2 ★ n_valid 保持原值（=0，**不半装载**）", post["n_valid"] == pre["n_valid"],
           "n_valid %d→%d%s" % (
               pre["n_valid"], post["n_valid"],
               "" if post["n_valid"] == pre["n_valid"] else
               "  ← 坏表被装载了 ⇒ 固件**没校验 fnv**（采信了段里那个值）"))
    allzero = all(x == 0 for x in post["entries"])
    record("T4.3 ★ entries[8] 不被写成半张表（全 0）", allzero,
           "entries=[%s]" % " ".join("%08X" % x for x in post["entries"]))
    d_bad = post["load_bad"] - pre["load_bad"]
    record("T4.4 ★ 拒绝可观测（DB_LOAD_BAD_N +1 且 DB_REJECT == DB_RC_CRC）",
           d_bad == 1 and post["reject"] == DB_RC_CRC,
           "load_bad %d→%d (Δ=%d)  DB_REJECT=%d(%s)%s" % (
               pre["load_bad"], post["load_bad"], d_bad, post["reject"],
               DB_RC_NAME.get(post["reject"], "?"),
               "" if (d_bad == 1 and post["reject"] == DB_RC_CRC) else
               "  ← 静默拒绝: 协议面上没有任何量变化 ⇒ 这条判据本身也不存在"))


# ══════════════════ T5 ══════════════════
def t5_idempotent(b):
    """同一条命令连发两次 ⇒ 第二次结果一致, 且不叠加副作用。

    ★ 参照物用**设备回读到的** `sA`（不是工具里的常量表）—— 与被测对象的对照必须是
      同一个来源, 否则"一致"可能只是两边都读了工具自己的期望值。
    """
    hdr("T5 幂等: 连发两次 op=23")
    seq, sA = submit(b, TABLE_A, PERIOD_A)
    if sA is None or sA["n_valid"] != len(TABLE_A):
        record("T5.0 表 A 就位（重装的段有内容可恢复）", False, tbl_str(sA))
        return
    ok, log, det = b.upload(PROG_BODY)
    if not ok:
        record("T5.0 上传裸程序", False, "%s" % det)
        return
    st = b.prog_status()
    print("        落盘长度 = %s（期望 %d = %d+48）" % (
        b.stored_len(st), len(PROG_BODY) + DB_SEG_LEN, len(PROG_BODY)))
    record("T5.0 表 A 就位且包内含段（前置）",
           b.stored_len(st) == len(PROG_BODY) + DB_SEG_LEN, "stored_len=%s" % b.stored_len(st))

    submit(b, [], PERIOD_B)            # 反空判据: 先把活表换成别的
    s_pre = b.snap()                   # ★ 基线: 用来量"每一次 op=23 各记了几次恢复"
    rc1, loaded1 = b.reload()          # 第 1 次
    ok1, s1 = wait_settle(b, 2.0)
    rc2, loaded2 = b.reload()          # 第 2 次
    ok2, s2 = wait_settle(b, 2.0)
    if s1 is None or s2 is None or s_pre is None:
        record("T5.1 连发两次 op=23", False, "读不到快照")
        return

    record("T5.1 两次 rc 相同且都为 OK", rc1 == DB_RC_OK and rc2 == DB_RC_OK,
           "第1次 rc=%s(%s) loaded=%s；第2次 rc=%s(%s) loaded=%s" % (
               rc1, RC_NAME.get(rc1, "?"), loaded1, rc2, RC_NAME.get(rc2, "?"), loaded2))
    eq, det = same_table(s1, s2)
    good_a, det_a = same_table(sA, s2)
    record("T5.2 ★ 两次的表状态完全一致 **且**等于表 A（不叠加副作用）", eq and good_a,
           "第1次 vs 第2次: %s；第2次 vs 表A: %s" % (det, det_a))
    record("T5.3 req_seq 单调不减 且 done_seq 追上它（没被搞乱/卡死）",
           s2["req_seq"] >= s1["req_seq"] and ok1 and ok2,
           "req_seq %d→%d (第2次 settle=%s)  done_seq %d→%d" % (
               s1["req_seq"], s2["req_seq"], ok2, s1["done_seq"], s2["done_seq"]))
    record("T5.4 两次都无拒绝（reject==0 且 rej_n 不变）",
           s2["reject"] == DB_RC_OK and s2["rej_n"] == s1["rej_n"] and s1["rej_n"] == s_pre["rej_n"],
           "reject=%d(%s) rej_n %d→%d→%d%s" % (
               s2["reject"], DB_RC_NAME.get(s2["reject"], "?"),
               s_pre["rej_n"], s1["rej_n"], s2["rej_n"],
               "" if s2["reject"] == DB_RC_OK else "  ← 第2次被当成了回退/CRC 错"))
    k, s3 = wait_growth(b, ("ok_n", "err_n"), dict(ok_n=s1["ok_n"], err_n=s1["err_n"]),
                        timeout=3.0)
    record("T5.5 两次之后表**仍在**被轮询（ok_n/err_n 还在涨）", k is not None,
           "ok_n %d→%s err_n %d→%s" % (
               s1["ok_n"], s3["ok_n"] if s3 else "?", s1["err_n"], s3["err_n"] if s3 else "?"))
    d1 = s1["load_ok"] - s_pre["load_ok"]
    d2 = s2["load_ok"] - s1["load_ok"]
    record("T5.6 每次 op=23 都各记一次恢复（load_ok 每次 +1 ⇒ 第二次确实也跑了）",
           d1 == 1 and d2 == 1,
           "load_ok %d→%d→%d（Δ=%d, %d）%s" % (
               s_pre["load_ok"], s1["load_ok"], s2["load_ok"], d1, d2,
               "" if (d1 == 1 and d2 == 1) else
               "  ← 第二次没真的执行恢复（或计数被叠加/漏记）"))


# ══════════════════ 收尾 ══════════════════
def cleanup(b, keep):
    hdr("收尾: 把「我留下了什么」变成可观测")
    if keep:
        s = b.snap()
        st = b.prog_status()
        print("  --keep 给定 ⇒ **不擦程序、不清表**。留在设备上的东西:")
        print("    绑定表: %s" % tbl_str(s))
        print("    程序:   stored_len=%s  deploy_seq=%s  deploy_routes=%s" % (
            b.stored_len(st), st["deploy_seq"] if st else "?", st["deploy_routes"] if st else "?"))
        print("  ★ 现在请做**真断电**验证（拔电/断电重上电, **不是** 0x13 RESET）:")
        print("      1) 断电 → 等 2 s → 上电")
        print("      2) 等板子起来, 然后跑下面这条（只读, 不改任何东西）:")
        ents = ",".join("0x%08X" % x for x in (s["entries"] if s else []))
        print("         python tools/h723_devbind_persist_test.py --port %s --verify-only \\"
              % (b.d.port))
        print("             --expect-nvalid %s --expect-period %s --expect-entries %s"
              % (s["n_valid"] if s else "?", s["period"] if s else "?", ents))
        print("      ★ op=23 验的是**装载路径**, 真断电才覆盖「上电时序/掉电瞬间」那一段。")
        return
    seq, s = submit(b, [], DB_PERIOD_DEF)
    record("收尾 提交空表（n_valid=0, 不留一个持续读某地址的绑定）",
           s is not None and s["n_valid"] == 0, tbl_str(s))
    sts, p = b.send(C_PROG_ERASE, b"", expect_len=4)
    rc = u32(p, 0) if (sts == "ACK" and len(p) >= 4) else None
    record("收尾 0x49 ERASE（清掉本次上传的程序; --keep 可跳过）", rc == DB_RC_OK,
           "sts=%s rc=%s" % (sts, RC_NAME.get(rc, rc)))


# ══════════════════ --verify-only（人工真断电之后）══════════════════
def verify_only(b, exp_nvalid, exp_period, exp_entries, cap_bit):
    hdr("--verify-only: 只读核对（人工真断电重上电之后跑这条）")
    sts, p = b.send(C_VERSION, b"", expect_len=4)
    if sts != "ACK":
        record("V1 链路活", False, "sts=%s" % sts)
        return 1
    cap = p[2] | (p[3] << 8)
    record("V1 能力字含持久化位", (cap_bit is not None) and bool(cap & cap_bit),
           "cap=0x%04X bit=%s" % (cap, ("0x%X" % cap_bit) if cap_bit else "?"))
    sts, p = b.send(C_ENGINE_STATUS, b"", expect_len=ENG_STATUS_LEN)
    if sts != "ACK" or len(p) < 27:
        record("V2 0x38 可读", False, "sts=%s" % sts)
        return 1
    b.shm = u32(p, 23)
    s = b.snap()
    if s is None or s["magic"] != DB_MAGIC_VAL:
        record("V2 SHM 区自证", False, tbl_str(s))
        return 1
    print("  当前: %s" % tbl_str(s))
    st = b.prog_status()
    print("  程序: stored_len=%s seq_a/b=%s/%s len_a/b=%s/%s deploy_seq=%s" % (
        b.stored_len(st), st["seq_a"] if st else "?", st["seq_b"] if st else "?",
        st["len_a"] if st else "?", st["len_b"] if st else "?",
        st["deploy_seq"] if st else "?"))

    record("V3 ★ 上电后表被恢复（n_valid > 0）", s["n_valid"] > 0,
           "n_valid=%d —— 0 表示这次上电**没有**恢复绑定表（段没落盘 / 装载没跑 / 段被拒）"
           % s["n_valid"])
    if exp_nvalid is not None:
        record("V4 n_valid == 期望 %d" % exp_nvalid, s["n_valid"] == exp_nvalid,
               "实际 %d" % s["n_valid"])
    if exp_period is not None:
        record("V5 period == 期望 %d" % exp_period, s["period"] == exp_period,
               "实际 %d" % s["period"])
    if exp_entries:
        want = (list(exp_entries) + [0] * DB_SLOTS)[:DB_SLOTS]
        record("V6 entries[8] == 期望", s["entries"] == want,
               "期望 [%s]\n        实际 [%s]" % (
                   " ".join("%08X" % x for x in want),
                   " ".join("%08X" % x for x in s["entries"])))
    k, s2 = wait_growth(b, ("ok_n", "err_n"), dict(ok_n=s["ok_n"], err_n=s["err_n"]),
                        timeout=3.0)
    record("V7 ★ 恢复的表真的在轮询（ok_n 或 err_n 在涨）", k is not None,
           "ok_n %d→%s  err_n %d→%s  last_err=%s" % (
               s["ok_n"], s2["ok_n"] if s2 else "?", s["err_n"], s2["err_n"] if s2 else "?",
               I2C_SM_ST_NAME.get(s["last_err"], "?") if s2 else "?"))
    return None


# ══════════════════ --dry-run 离线自检 ══════════════════
def dry_run(args):
    hdr("--dry-run: 离线自检（不打开串口, 不碰设备）")
    defs, err = load_header_facts()
    if err:
        record("D0 解析 src/*.h", False, err)
        return 1
    record("D0 解析 src/*.h", True, "transport.h / engine.h / dev_bind.h / prog_store.h")

    bit, note = cap_bit_from_header(defs, args.cap_bit)
    record("D0 ★ 能力位从源码解析出（**不写死**）", bit is not None and bit != 0, note)
    if bit is None:
        return 1
    ok_static = t0_static(defs)
    if not ok_static:
        return 1

    # D1 段构造: 逐字段自检 + fnv 独立重算
    seg = build_segment(PERIOD_A, TABLE_A)
    exp_ents = (list(TABLE_A) + [0] * DB_SLOTS)[:DB_SLOTS]
    d1 = (len(seg) == DB_SEG_LEN and u32(seg, 0) == DB_MAGIC_VAL
          and u32(seg, 4) == DB_SEG_LEN and u32(seg, 8) == PERIOD_A
          and u32(seg, 12) == db_crc_words(exp_ents)
          and [u32(seg, 16 + 4 * i) for i in range(DB_SLOTS)] == exp_ents)
    record("D1 段构造逐字段自检（magic/seg_len/period/fnv/entries[8] == 48B）", d1,
           "fnv=0x%08X entries=%s" % (u32(seg, 12),
                                      " ".join("%08X" % x for x in exp_ents)))

    # D2 镜像 unpack 的三态必须真的落在三个不同分支（否则 T1/T3/T4 是同一个实验）
    k_ok, _ = seg_probe(PROG_BODY + seg)
    k_none, _ = seg_probe(PROG_BODY)
    bad = build_segment(PERIOD_A, TABLE_A, fnv=u32(seg, 12) ^ 0xDEADBEEF)
    k_bad, _ = seg_probe(PROG_BODY + bad)
    record("D2 ★ 三个载荷分别落在 ok/none/bad 三个分支（T1/T3/T4 不是同一个实验）",
           (k_ok, k_none, k_bad) == ("ok", "none", "bad"),
           "含段=%s 无段=%s 段坏=%s" % (k_ok, k_none, k_bad))

    # D3 无段载荷的"尾部 48B"不得**碰巧**以 'DBND' 开头（否则 T3 会假变成"有段"）
    tail = PROG_BODY[-DB_SEG_LEN:] if len(PROG_BODY) >= DB_SEG_LEN else PROG_BODY
    record("D3 老包载荷的尾部 48B 不以 'DBND' 开头（T3 不会被误判成「有段」）",
           u32(tail, 0) != DB_MAGIC_VAL,
           "尾部首 4B = %08X（'DBND'=0x%08X）；体长=%d" % (
               u32(tail, 0), DB_MAGIC_VAL, len(PROG_BODY)))

    # D4 分片计划覆盖 [0,total) 恰好一次且顺序连续（off 必须 == 已收字节数）
    ok_all, det_all = True, []
    for tot in (54, 102, 140, 141, 6150, 7680):
        plan = chunk_plan(tot, 140)
        off = 0
        good = True
        for o, n in plan:
            if o != off:
                good = False
                break
            off += n
        good = good and off == tot and sum(n for _, n in plan) == tot
        ok_all = ok_all and good
        det_all.append("%d→%d片%s" % (tot, len(plan), "" if good else "(坏)"))
    record("D4 上传分片恰好覆盖 [0,total) 一次、offset 连续（0x46 的 off != got 会被拒）",
           ok_all, " ".join(det_all))

    # D5 期望长度算式与固件侧对上（裸长 / +48）
    record("D5 长度算式: 裸长 %d = 6+(nr+np+ns)*16; 含段 = %d"
           % (len(PROG_BODY), len(PROG_BODY) + DB_SEG_LEN),
           len(PROG_BODY) == program_body_len(),
           "T1/T4 期望落盘长度=%d, T3 期望=%d" % (
               len(PROG_BODY) + DB_SEG_LEN, len(PROG_BODY)))

    # ── D6..D8: **反空判据自证** —— 故意把每类判据弄红, 看它是否真的会红 ──
    #    (本项目铁律: "判据必须能失败, 而且要能证明它会红")
    sab = list(_TOOL_CONSTS)
    for i, (n, _v) in enumerate(sab):
        if n == "DB_LOAD_BAD_N":
            sab[i] = (n, _v + 4)
    bad = static_mismatches(defs, sab)
    record("D6 ★ 把工具常量改错一处 ⇒ T0.3 必须报红（证明它不是恒真判据）",
           len(bad) == 1 and "DB_LOAD_BAD_N" in bad[0],
           "注入 DB_LOAD_BAD_N+4 ⇒ 报出 %d 处: %s" % (len(bad), bad[:1]))

    seg_m = bytearray(seg)
    seg_m[16 + 4 * 1] ^= 0x01                       # 改 entries[1] 一个位（fnv 就不对了）
    k_mut, _ = seg_probe(PROG_BODY + bytes(seg_m))
    seg_n = bytearray(seg)
    seg_n[0] ^= 0xFF                                # 破掉 magic
    k_mag, _ = seg_probe(PROG_BODY + bytes(seg_n))
    record("D7 ★ 段判定三态可移（改 entries⇒bad, 破 magic⇒none, 原样⇒ok）",
           (k_mut, k_mag) == ("bad", "none"),
           "改 entries=%s 破 magic=%s 原样=%s；fnv 对 entries 敏感=%s" % (
               k_mut, k_mag, k_ok, db_crc_words([1, 2, 3, 4, 5, 6, 7, 8])
               != db_crc_words([1, 2, 3, 4, 5, 6, 7, 9])))
    return None


# ══════════════════ main ══════════════════
PROG_BODY = b""
TABLE_A = []
PERIOD_A = 5      # ★ 非默认值(默认 10) —— 否则"恢复了 period"这条判据可能假 PASS
PERIOD_B = 20     # 换表时用的另一个值, 与 A 不同 ⇒ 恢复必须来自段


def main():
    global PROG_BODY, TABLE_A
    ap = argparse.ArgumentParser(
        description="H723 GAP-11: 具名设备绑定表随程序包持久化 上机验收（只用协议）")
    ap.add_argument("--port", default=None, help="串口号（不给则按能力字自动找板子）")
    ap.add_argument("--dry-run", action="store_true",
                    help="离线自检: 解析 src/*.h + 段构造/分片/镜像判定, **不打开串口**")
    ap.add_argument("--verify-only", action="store_true",
                    help="只读核对（人工真断电重上电之后跑这条; 不写、不擦、不重装载）")
    ap.add_argument("--expect-nvalid", type=int, default=None, help="--verify-only: 期望 n_valid")
    ap.add_argument("--expect-period", type=int, default=None, help="--verify-only: 期望 period")
    ap.add_argument("--expect-entries", default=None,
                    help="--verify-only: 期望的 8 个条目（十六进制, 逗号分隔; 不足补 0）")
    ap.add_argument("--expect-entry", action="append", default=None,
                    help="同上, 但可重复给出（--expect-entry 0x... --expect-entry 0x...）")
    ap.add_argument("--cap-bit", type=lambda s: int(s, 0), default=CAP_BIT_DEFAULT,
                    help="覆盖能力位（默认从 src/transport.h 的 DCL_CAP_DEVBIND_PERSIST 解析）")
    ap.add_argument("--keep", action="store_true",
                    help="跑完**不擦程序、不清表**, 便于人工做真断电验证（并打印期望值"
                         "与 --verify-only 命令行）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每帧")
    a = ap.parse_args()

    PROG_BODY = build_prog(2, 1)
    TABLE_A = [entry(1, DST_A, AS5600_LEN, AS5600_REG, AS5600_ADDR7),
               entry(1, DST_B, 1, AS5600_REG, AS5600_ADDR7)]

    if a.dry_run:
        rc = dry_run(a)
        return _finish(rc if rc is not None else None)

    if a.verify_only and a.keep:
        print("!! --verify-only 与 --keep 互斥")
        return 1

    # ── 串口（★ 独占资源）──
    port = a.port
    if not port:
        try:
            port = client_mod().find_board()
        except Exception as e:
            print("!! 自动找板子失败: %s" % e)
            print("   用 --port 指定；若怀疑口被占, 先确认没有别的 h723_*.py 在跑"
                  "（本项目两个 CH340 通到同一块板子, 抢口会表现为「板子没响应」）。")
            return 1
    print("端口: %s @115200   (独占资源: 若打不开, 先查有没有别的进程占着)" % port)
    try:
        b = Board(port, verbose=a.verbose)
    except Exception as e:
        print("!! 打不开串口 %s: %s" % (port, e))
        print("   最可能: ① 口被别的进程占着（另一个 h723_*.py / 串口助手 / pyocd）")
        print("           ② 口写错（本机两个 CH340, 常挑错口）  ③ 板子没上电")
        print("   先跑 tools/h723_revive.py 看诊断, 或用 --port 指定。")
        return 1

    rc = None
    try:
        defs, err = load_header_facts()
        if err:
            print("!! 读不到 src/*.h: %s（前置不成立）" % err)
            return 1
        bit, note = cap_bit_from_header(defs, a.cap_bit)
        print("能力位: %s" % note)
        if not t0_static(defs):
            print("\n!! 工具常量与源码不一致 ⇒ 前置不成立（此时读到的每个数都不可信）")
            return 1

        if a.verify_only:
            ents = []
            if a.expect_entries:
                for x in re.split(r"[,\s]+", a.expect_entries.strip()):
                    if x:
                        ents.append(int(x, 0))
            if a.expect_entry:
                ents += [int(x, 0) for x in a.expect_entry]
            rc = verify_only(b, a.expect_nvalid, a.expect_period, ents or None, bit)
        else:
            if not t0_preflight(b, bit):
                print("\n!! T0 前置不成立 —— **这不算通过**, 也不代表功能有问题:")
                print("   要么链路/端口不对, 要么固件是**老版本**(未声明持久化能力位)。")
                return 1
            ok1, tab_a = t1_restore(b)
            present, _ = t2_polling(b, tab_a)
            t3_no_segment(b)
            t4_bad_fnv(b)
            t5_idempotent(b)
            cleanup(b, a.keep)
            print("\n  器件: %s" % (
                "**实际读到**（ok_n 在涨 ⇒ 接有 AS5600）" if present else
                "未读到（只有 err_n 在涨 / 或恢复失败）—— 见 T2.2 的标注"))
    except KeyboardInterrupt:
        print("\n!! 用户中断")
        rc = 1
    finally:
        try:
            b.close()
        except Exception:
            pass
    return _finish(rc)


def _finish(rc):
    print("\n" + "=" * 78)
    print("  摘要:  PASS %d / FAIL %d / SKIP %d" % (_N_PASS, _N_FAIL, _N_SKIP))
    if _N_FAIL:
        rc = 2
    elif _N_SKIP or rc == 1:
        rc = 1
    else:
        rc = 0
    print("  退出码 %d = %s" % (rc, {0: "全部通过", 1: "环境错误/前置不成立/**有判据没跑到**",
                                     2: "有 FAIL"}.get(rc, "?")))
    print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
