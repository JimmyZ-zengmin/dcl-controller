/* dev_bind.c — 具名设备绑定表的服务方。**契据见 docs/REF-program-contract.md §3.8**
 *
 * ## 它做两件事（都在主循环，理由见 §3.8.6）
 *   `dev_bind_submit()`  —— 校验 PC 下发的表（crc/范围/序号），通过则**整表替换**；否则**保持上一次绑定**
 *   `dev_bind_service()` —— round-robin 轮询一个槽：发起状态机事务 → 完成后把值写进 `SENSOR[dst]`
 *
 * ## 三条"绝不静默"的纪律（都来自本项目踩过的坑）
 *   ① **拒绝就拒绝**（照 `SRC_HMI` 先例）：非法表 ⇒ 不绑定 + 写原因码；**绝不半装载**
 *   ② **失败保留上一次的值**（照 `as5600_poll`）：读失败写成 0 会让"没读到"伪装成"值=0"
 *   ③ **拒绝也要回 `done_seq`**：否则上位机分不清"还在等"与"被拒了"——**判据必须能终止**
 *
 * ## ★★ 四个"会静默错"的位置（本文件逐处挡住，每条都有源码注释说明为什么）
 *   ① **表在飞期间被换** ⇒ 收尾时会拿**新表**的 lane 去解释**旧事务**的数据（索引张冠李戴）
 *   ② **事务结果归属** ⇒ 只查 `status==OK` 会把**别人的**读数当成自己的（见 i2c_sm.h 的令牌）
 *   ③ **一个槽两个语义** ⇒ `DB_ERR_N` 被"轮询失败"与"上传被拒"共用, 判据无法解释
 *   ④ **序号不单调** ⇒ 重放一张旧表就能把现场打回旧配置, 且没有任何观测量表明"这是旧表"
 */
#include "dev_bind.h"
#include <stddef.h>
#include "engine.h"
#include "i2c_sm.h"
#include "i2c_bb.h"     /* ★ 总线独占门的读取口（门的定义在 i2c_bb.c = 资源处）*/

static uint8_t *s_shm = NULL;

/* 已生效的绑定（**解析后的本地副本** —— 照 bb_map_bind: init 一次性解析, 不在拍内查表）*/
typedef struct { uint8_t dev, dst, len, reg, addr7; } db_lane_t;
static db_lane_t s_lane[DB_SLOTS];
static uint32_t  s_n = 0u;          /* 生效槽数（= SHM 的 DB_N_VALID）*/
static uint32_t  s_rr = 0u;         /* round-robin 游标 */
static uint32_t  s_last_start = 0u; /* 上次发起(或被拒)的拍号 —— 速率闸 */
static uint32_t  s_busy = 0u;       /* 1 = 本模块有一个事务在飞 */
static uint32_t  s_idx = 0u;        /* 在飞的槽号 */
static uint32_t  s_my_req = 0u;     /* ★★ 我发出的那次事务的**受理序号**（归属令牌, 见 i2c_sm.h）*/

/* ── 待生效的表（在飞事务期间提交的）—— 见提交函数的 ① 号说明 ── */
static db_lane_t s_pend[DB_SLOTS];
static uint32_t  s_pend_n     = 0u;
static uint32_t  s_pend_seq   = 0u;    /* 已暂存的那一号 */
static uint32_t  s_pend_valid = 0u;

volatile uint32_t g_db_ok_n = 0u, g_db_err_n = 0u, g_db_last_err = 0u,
                  g_db_skip_n = 0u, g_db_rej_n = 0u,
                  g_db_load_ok_n = 0u, g_db_load_bad_n = 0u;

/* FNV-1a —— **与上位机同算法**（照 bb_map_sum 的用途: 判"表与固件是不是同一份映射"）*/
static uint32_t db_crc(const uint8_t *shm)
{
    uint32_t h = 2166136261u;
    for (uint32_t i = 0u; i < DB_SLOTS; i++) {
        h ^= SHM_U32(shm, DB_ENTRIES + i * 4u);
        h *= 16777619u;
    }
    return h;
}

static void db_local_clear(void)
{
    for (uint32_t i = 0u; i < DB_SLOTS; i++) {
        s_lane[i].dev = DB_DEV_EMPTY; s_lane[i].dst = 0u;
        s_lane[i].len = 0u; s_lane[i].reg = 0u; s_lane[i].addr7 = 0u;
        s_pend[i].dev = DB_DEV_EMPTY; s_pend[i].dst = 0u;
        s_pend[i].len = 0u; s_pend[i].reg = 0u; s_pend[i].addr7 = 0u;
    }
    s_n = 0u; s_rr = 0u; s_busy = 0u; s_idx = 0u; s_last_start = 0u; s_my_req = 0u;
    s_pend_n = 0u; s_pend_seq = 0u; s_pend_valid = 0u;
}

/* ★★ 必须在 `cold_start_reset()` 里被调用（本项目"新增域必须登记到单一入口"的纪律）。
 *   登记在这里的理由与 mb_config / macro_reset / fault_init 完全一样:
 *   `cold_start_reset` 是**唯一**的 SHM 整段清零入口（上电 / 0x13 RESET / reinit 都经过它）。
 *   ★ 若只在开机写一次 magic, 那么**一次普通的 0x13 RESET 之后 magic 就变 0** ——
 *     于是"区存在自证"会**撒谎**（上位机以为没有这个区）。这是本模块自带的第一个判据。 */
void dev_bind_reset(uint8_t *shm)
{
    for (uint32_t i = 0u; i < OFF_DEV_BIND_SZ; i++) { shm[OFF_DEV_BIND + i] = 0u; }
    SHM_U32(shm, DB_MAGIC)  = DB_MAGIC_VAL;
    SHM_U32(shm, DB_PERIOD) = DB_PERIOD_DEF;
    s_shm = shm;
    db_local_clear();
    g_db_ok_n = 0u; g_db_err_n = 0u; g_db_last_err = 0u;
    g_db_skip_n = 0u; g_db_rej_n = 0u;
    g_db_load_ok_n = 0u; g_db_load_bad_n = 0u;
}

void dev_bind_init(uint8_t *shm) { s_shm = shm; }

/* 把一张已校验过的表换上去（**唯一**的换表点）*/
static void db_apply(const db_lane_t *t, uint32_t n, uint32_t seq)
{
    for (uint32_t i = 0u; i < n; i++) { s_lane[i] = t[i]; }
    s_n = n; s_rr = 0u;
    /* ★★ `N_VALID` **只在这里写**（"当前生效"的登记数）。
     *   早先它在提交函数里写、还被 `s_busy!=0` 挡掉 ⇒ 会出现"表换了但登记数还是旧的"
     *   （审计：顺序依赖导致计数永远停旧值）。现在它跟着**生效**这一步走, 二者不可能不一致。 */
    SHM_U32(s_shm, DB_N_VALID) = s_n;
    SHM_U32(s_shm, DB_DONE_SEQ) = seq;      /* ★ 生效才算"回执"（就绪门语义）*/
}

/* ── 校验并绑定 ── */
void dev_bind_submit(void)
{
    if (s_shm == NULL) { return; }
    uint32_t rq = SHM_U32(s_shm, DB_REQ_SEQ);
    uint32_t dn = SHM_U32(s_shm, DB_DONE_SEQ);
    if (rq == 0u) { return; }              /* 尚未提交 */
    if (rq == dn) { return; }              /* 已生效 ⇒ 幂等, 不重复处理 */
    if (s_pend_valid != 0u && s_pend_seq == rq) { return; }   /* 已暂存同一号 ⇒ 别重复校验 */

    /* ★★★ ④ 序号单调性（契约 §3.8.4 码 2）—— **必须真的实现, 不能只留常量**。
     *   只管"表合不合法"是不够的: 一张**旧表**配上**旧序号**, 字段全部合法、crc 也对
     *   ⇒ 会被无条件接受并整表替换, **把现场打回旧配置**。而且任何观测量都不会说
     *   "这是一张旧表"（`REJECT=0`、`N_VALID` 合法、`DONE_SEQ` 跟上了）。
     *   ⇒ 判据: 用**有符号差**判回退（`(int32_t)(rq - dn) < 0`）, 天然处理 u32 回绕。
     *   ★ `dn == 0` 是**新纪元**（刚冷启动/从未生效过）⇒ 任何非 0 序号都算"新的", 不判回退。
     *     没有这个例外就有个不必要的约束: 首张表若用 `req_seq >= 2^31`, 会被误判成回退
     *     （审计指出的边界）。有了它, "序号只要单调递增即可", 与起始值无关。
     *   ★ 回退被拒时**不回 `done_seq`**: 它代表"当前已生效的最新号", 回退它会破坏单调性。
     *     上位机只须看 `reject != 0 && done_seq != rq` 即可判定并**终止等待**（判据能终止）。 */
    if (dn != 0u && (int32_t)(rq - dn) < 0) {
        SHM_U32(s_shm, DB_REJECT) = DB_RC_SEQ;
        g_db_rej_n++;
        SHM_U32(s_shm, DB_REJ_N)  = g_db_rej_n;
        return;
    }

    uint32_t rc = DB_RC_OK;
    /* ① crc —— **"半个表"最危险**: 没有它, "写了一半"与"完整的表"在固件看来一模一样 */
    if (db_crc(s_shm) != SHM_U32(s_shm, DB_CRC)) { rc = DB_RC_CRC; }

    /* ② 逐槽范围校验 + 解析（先全部核过, 再整表替换 —— 这是"绝不半装载"的实现方式）*/
    db_lane_t tmp[DB_SLOTS];
    uint32_t   tn = 0u;
    if (rc == DB_RC_OK) {
        for (uint32_t i = 0u; i < DB_SLOTS; i++) {
            uint32_t e = SHM_U32(s_shm, DB_ENTRIES + i * 4u);
            uint8_t dev = (uint8_t)((e >> 28) & 0xFu);
            if (dev == DB_DEV_EMPTY) { continue; }              /* 空槽: 合法, 跳过 */
            uint8_t dst = (uint8_t)((e >> 24) & 0xFu);
            uint8_t len = (uint8_t)((e >> 16) & 0xFFu);
            uint8_t reg = (uint8_t)((e >>  8) & 0xFFu);
            uint8_t a7  = (uint8_t)( e        & 0xFFu);
            if (dev > DB_DEV_I2C_RD)   { rc = DB_RC_DEV;  break; }
            if (a7 > 0x7Fu)            { rc = DB_RC_ADDR; break; }
            if (len == 0u || len > 2u) { rc = DB_RC_LEN;  break; }
            if (dst > 15u)             { rc = DB_RC_DST;  break; }
            tmp[tn].dev = dev; tmp[tn].dst = dst; tmp[tn].len = len;
            tmp[tn].reg = reg; tmp[tn].addr7 = a7;
            tn++;
        }
    }

    if (rc != DB_RC_OK) {
        /* ★ 保持上一次生效的绑定（**绝不半装载**）；`DB_REJECT` 记码, `DB_REJ_N` 计数。
         * ★ 这里**不碰** `DB_ERR_N` / `DB_LAST_ERR`: 那两个量回答的是"**轮询**出了什么事",
         *   被一张非法上传表污染之后, "err 保持 0"这条判据就再也解释不清了（一个计数一个问题）。 */
        g_db_rej_n++;
        SHM_U32(s_shm, DB_REJ_N)   = g_db_rej_n;
        SHM_U32(s_shm, DB_REJECT)  = rc;
        /* ★★ ③ 拒绝也必须回 `done_seq`（`rq == dn` 是上位机的"已处理"判据）。
         *   否则判据永远等下去 —— "判据必须能终止"（契约 §3.8.4 末段）。 */
        SHM_U32(s_shm, DB_DONE_SEQ) = rq;
        return;
    }

    SHM_U32(s_shm, DB_REJECT) = DB_RC_OK;   /* 最近一次提交被接受 */

    if (s_busy == 0u) {
        db_apply(tmp, tn, rq);              /* 无在飞 ⇒ 立即生效 */
        return;
    }

    /* ★★★ ① 有事务在飞 ⇒ **暂存, 绝不立刻换表**。
     *   为什么（这是审计挖出来的真缺陷，本文件第一版就是错的）:
     *     换表会同时重置 `s_rr`，而收尾代码用的是**当时的** `s_idx` 去查**新的** `s_lane`：
     *       · 新表槽数更少 ⇒ `s_idx` 越界读 `s_lane[]`（读到陈旧/垃圾条目）
     *       · 新表有效槽更多 ⇒ 同一个 `s_idx` 指向**另一个器件** ⇒
     *         读数被写进**另一个** `SENSOR[dst]`
     *     两种都**无任何错误迹象**（`REJECT=0`、计数照涨）。判据: "在飞期间能不能换表"。
     *   ⇒ 改成: 暂存 + **延后 `done_seq`**。上位机看到 `done_seq != rq` 就知道还没生效,
     *     而等待是**有界**的（在飞事务最多 6+n 拍 ≈ 800 µs）⇒ 判据仍能终止。 */
    for (uint32_t i = 0u; i < tn; i++) { s_pend[i] = tmp[i]; }
    s_pend_n     = tn;
    s_pend_seq   = rq;
    s_pend_valid = 1u;
    /* ★ 注意这里**不写** `DONE_SEQ` / `N_VALID` —— 等收尾后再由 `db_apply()` 写。 */
}

/* ── 轮询一个槽（round-robin）── */
void dev_bind_service(uint32_t tick)
{
    if (s_shm == NULL) { return; }

    /* ① 在飞事务已收尾 ⇒ 现在是"换表"的安全点（旧 lane 已用完, 新 lane 尚未被引用）*/
    if (s_busy == 0u && s_pend_valid != 0u) {
        db_apply(s_pend, s_pend_n, s_pend_seq);
        s_pend_valid = 0u;
    }

    uint32_t period = SHM_U32(s_shm, DB_PERIOD);
    if (period == 0u || period > DB_PERIOD_MAX) { period = DB_PERIOD_DEF; }

    /* ② 有一个在飞 ⇒ 只做收尾 */
    if (s_busy != 0u) {
        if (i2c_sm_status() == I2C_SM_ST_BUSY) { return; }      /* 还在跨拍推进 */
        /* ★★★ 收尾必须走**完成记录**（2026-09-16 第二轮审计后修正）。
         *   只看 `status == OK` 不够: 那只能证明"**有人**跑完了一次全 ACK 的事务",
         *   证明不了"那一次**是我发的**"。本状态机只有一个 `s_rbuf`, 另一个使用者
         *   （`i2c_shm_service`）**可以在我收尾之前插入并完成**, 把他的读数留在 `s_rbuf` 里。
         *   ★★ 而且**只加令牌也不够**（这是第二轮的发现）: `s_status` 是**实时态**,
         *     一个**被拒**的请求（门忙 / BADARG）会覆写它 ⇒ 会出现"令牌匹配但状态已被改"，
         *     于是收尾方**丢弃自己的好数据并计一次 err**。
         *   ⇒ 正解 = 完成记录 `{done_req, done_status, done_len}`（完成那刻快照、
         *     受理新请求即失效），并用 `take_result(my, …)` 取数 —— 全程不碰实时字段。 */
        if (i2c_sm_done_req() != s_my_req) {
            /* 结果被后来者覆盖 / 记录已失效 ⇒ **丢弃并重发**（绝不当成功用）。
             * ★ 计入 skip 而非 err: 这是**调度现象**（被别人抢先收尾）, 不是器件/通信故障。
             *   混进 err 会让"err 在涨"这条判据无法解释 ⇒ 判据作废（一个计数只回答一个问题）。 */
            s_busy = 0u;
            g_db_skip_n++;
            SHM_U32(s_shm, DB_SKIP_N) = g_db_skip_n;
            return;
        }
        s_busy = 0u;
        uint8_t d[I2C_SM_MAX_DATA];
        uint32_t got = i2c_sm_take_result(s_my_req, d, (uint32_t)I2C_SM_MAX_DATA);
        if (got >= s_lane[s_idx].len) {          /* take_result 只在**完成状态为 OK** 时给数据 */
            float fv = (s_lane[s_idx].len == 1u)
                     ? (float)d[0]
                     : (float)(((uint32_t)d[0] << 8) | (uint32_t)d[1]);   /* 大端, 与 AS5600 一致 */
            *(volatile float *)(s_shm + OFF_SENSOR_MAP + (uint32_t)s_lane[s_idx].dst * 4u) = fv;
            g_db_ok_n++;
            SHM_U32(s_shm, DB_OK_N) = g_db_ok_n;
        } else {
            /* ★ 失败**保留上一次的值**（不写 0）—— 把"没读到"伪装成"值=0"会让闭环跑飞 */
            g_db_err_n++;
            SHM_U32(s_shm, DB_ERR_N)   = g_db_err_n;
            /* ★ 记**完成时**的状态（`done_status`），不是实时 `status` —— 后者可能已被
             *   别人的被拒请求改掉, 那样记下来的"原因"就是错的。 */
            SHM_U32(s_shm, DB_LAST_ERR)= i2c_sm_done_status();
        }
        return;
    }

    /* ③ 无绑定 ⇒ 不动作 */
    if (s_n == 0u) { return; }

    /* ④ 速率闸: 距上次发起(或被拒)不足 period 拍就等 */
    if ((s_last_start != 0u) && ((uint32_t)(tick - s_last_start) < period)) { return; }

    /* ⑤ ★★ 总线被**阻塞路径**（AS5600 等）占用 ⇒ 本轮**连发起都不发起**。
     *   门本身能拒（`i2c_sm_request` 内部会 acquire 失败并返回 0）, 但硬去撞有两个坏处:
     *     ① 每小时数千次必然失败的尝试;  ② 会把"总线忙"计成 db 的 err ⇒ **判据退化**
     *        （err 必须只回答"器件/通信出了什么事"）。
     *
     *   ★★★ 判据必须是"**阻塞路径**在占", **不是**"SM 没在占"（我第一版写反了, 实测抓到）:
     *     本模块**自己就走 SM 这条路** ⇒ 空闲时 `owner == I2C_OWNER_NONE`, 而 `NONE != SM`
     *     ⇒ 照抄 main.c 里 AS5600 那句 `!= I2C_OWNER_SM` 会让**每一轮**都判"忙"而跳过,
     *       结果是"提交全部成功、`n_valid` 正常、而 `ok_n`/`err_n` 永远不动"。
     *     抓到它的正是**专为此加的那个计数**: `skip_n` 一路涨到 3327 并冻结在
     *     "表空 ⇒ 提前 return"那一刻 —— 判据把一次静默空转变成了一个**可读的指纹**。
     *   ★ 教训一般化: **同一个表达式在两个调用点语义可以不同** ——
     *     "我要避开的人" 与 "我怕被谁避开" 不是一回事（同族: 守卫必须住在资源处）。*/
    uint32_t owner = i2c_bus_owner();
    /* 判据写成"**有人在占, 且不是我**"而不是"== BLOCKING":
     * 将来若出现第三个 owner, 前者仍然正确, 后者会静默放行（"枚举式判据"的固有脆性）。 */
    if (owner != I2C_OWNER_NONE && owner != I2C_OWNER_SM) {
        s_last_start = tick;
        g_db_skip_n++;
        SHM_U32(s_shm, DB_SKIP_N) = g_db_skip_n;
        return;
    }

    /* ⑥ 找下一个**有效的**槽（空槽不占轮询机会）*/
    for (uint32_t k = 0u; k < DB_SLOTS; k++) {
        uint32_t idx = (s_rr + k) % s_n;
        if (s_lane[idx].dev == DB_DEV_EMPTY) { continue; }
        s_rr = (idx + 1u) % s_n;
        s_idx = idx;
        s_last_start = tick;               /* ★ 被拒也记 —— 否则会变成"每圈无限重试"的忙等 */
        if (i2c_sm_request(s_lane[idx].addr7, I2C_SM_OP_READ, s_lane[idx].reg,
                           NULL, s_lane[idx].len) != 0u) {
            /* ★★ 受理成功 ⇒ 立刻记下**我这一号**（`i2c_sm_request` 内部已 `req_n++`）,
             *    收尾时用它核对结果归属。漏了这一步 = 上面那条静默错数据的路径。 */
            s_my_req = g_i2c_sm_req_n;
            s_busy = 1u;
        } else {
            uint32_t st = i2c_sm_status();
            /* ★★ 两种"没轮到我"都必须算 **skip 而不是 err**（第二轮审计挖出的第二条漏洞）:
             *   · `GATE_BUSY` = 阻塞路径占着总线（上面 ⑤ 已提前挡掉，这里是收窄后的窗口）
             *   · `BUSY`      = **别人的事务正在飞**（`i2c_sm_request` 因 `s_phase` 非空闲而返回 0，
             *                   **且它不改 `s_status`** ⇒ 状态仍是 BUSY）
             *     后者在**同一圈里就稳定复现**：`i2c_shm_service()` 排在 `dev_bind_service()`
             *     **之前**（main.c），它一旦发起，本函数这一圈的请求必然撞上 BUSY。
             *   ⇒ 若把 BUSY 计成 err, 那么"err 只回答器件/通信问题"这条判据**当场失效**
             *     （err 会因为另一个使用者的正常活动而涨）。 */
            if (st == I2C_SM_ST_GATE_BUSY || st == I2C_SM_ST_BUSY) {
                g_db_skip_n++;
                SHM_U32(s_shm, DB_SKIP_N) = g_db_skip_n;
            } else {
                /* BADARG 等 = **真异常**（合法表已经过 addr7/len 范围校验, 本不该出现）*/
                g_db_err_n++;
                SHM_U32(s_shm, DB_ERR_N)    = g_db_err_n;
                SHM_U32(s_shm, DB_LAST_ERR) = st;
            }
        }
        return;
    }
}

uint32_t dev_bind_valid_count(void) { return s_n; }
uint32_t dev_bind_ok_count(void)    { return g_db_ok_n; }
uint32_t dev_bind_err_count(void)   { return g_db_err_n; }
uint32_t dev_bind_skip_count(void)  { return g_db_skip_n; }
uint32_t dev_bind_rej_count(void)   { return g_db_rej_n; }
uint32_t dev_bind_load_ok(void)     { return g_db_load_ok_n; }
uint32_t dev_bind_load_bad(void)    { return g_db_load_bad_n; }

/* ══════════ 随程序包持久化（GAP-11 / 契约 §3.8.5）══════════
 * 格式与"为什么放载荷尾部"的完整推导见 `dev_bind.h` 的同名小节。这里只写实现要点：
 *   `dev_bind_pack`   —— 只在**确有绑定**时附加（否则原样返回 ⇒ 老包语义不变）
 *   `dev_bind_unpack` —— **不自己校验条目**, 写回 SHM 后调 `db_submit()`（= 上传路径同一个函数）
 */
static void put32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu); p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu); p[3] = (uint8_t)((v >> 24) & 0xFFu);
}
static uint32_t get32le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

uint32_t dev_bind_pack(uint8_t *payload, uint32_t len, uint32_t cap)
{
    if (s_shm == NULL || payload == NULL) { return len; }
    /* ★★ 只在**确有绑定**时附加。若恒定附加一个空段, 就会破坏"老包行为逐字节相同"
     *   这条兼容性承诺（载荷长度会凭空多 48 字节, 上位机对账会发现"我没发这么多"）。 */
    if (s_n == 0u) { return len; }
    if (len + DB_SEG_LEN > cap) { return len; }        /* 放不下 ⇒ 宁可不带, 也不截断 */

    uint8_t *seg = payload + len;
    put32le(seg +  0, DB_MAGIC_VAL);                   /* 'DBND'（与运行时的区自证同值）*/
    put32le(seg +  4, DB_SEG_LEN);
    put32le(seg +  8, SHM_U32(s_shm, DB_PERIOD));
    put32le(seg + 12, db_crc(s_shm));                  /* 与运行时**同一算法** ⇒ 装载时可独立复核 */
    for (uint32_t i = 0u; i < DB_SLOTS; i++) {
        put32le(seg + 16u + i * 4u, SHM_U32(s_shm, DB_ENTRIES + i * 4u));
    }
    return len + DB_SEG_LEN;
}

uint32_t dev_bind_unpack(const uint8_t *payload, uint32_t len)
{
    if (s_shm == NULL || payload == NULL) { return DB_SEG_NONE; }
    if (len < DB_SEG_LEN) { return DB_SEG_NONE; }      /* 老包（没有这一段）—— 正常, 不是错误 */
    /* ★ 段的位置：载荷**尾部**最后 48 字节。先按"尾部"取, 再看 magic —— 这样即使
     *   将来载荷前面又加了别的东西, 判据也不变（只依赖"段在最后"这一条约定）。 */
    const uint8_t *seg = payload + len - DB_SEG_LEN;
    if (get32le(seg + 0) != DB_MAGIC_VAL) { return DB_SEG_NONE; }   /* 没有段 */

    /* 段在 ⇒ 从这里开始, 任何不符都必须**明确拒绝且不半装载**（可观测：DB_LOAD_BAD_N + DB_REJECT）*/
    uint32_t bad = 0u;
    if (get32le(seg + 4) != DB_SEG_LEN) { bad = 1u; }
    if (bad == 0u) {
        for (uint32_t i = 0u; i < DB_SLOTS; i++) {
            SHM_U32(s_shm, DB_ENTRIES + i * 4u) = get32le(seg + 16u + i * 4u);
        }
        /* ★ 现场重算 fnv（**不是**采信段里那个值）—— 采信它等于没校验。 */
        if (db_crc(s_shm) != get32le(seg + 12)) { bad = 1u; }
    }
    if (bad != 0u) {
        /* 段坏了 ⇒ 把条目**清回原样**（不留半张表）+ 明确拒绝 + 计数 */
        for (uint32_t i = 0u; i < DB_SLOTS; i++) { SHM_U32(s_shm, DB_ENTRIES + i * 4u) = 0u; }
        SHM_U32(s_shm, DB_CRC)    = db_crc(s_shm);
        SHM_U32(s_shm, DB_REJECT) = DB_RC_CRC;
        g_db_load_bad_n++;
        SHM_U32(s_shm, DB_LOAD_BAD_N) = g_db_load_bad_n;
        return DB_SEG_BAD;
    }

    uint32_t period = get32le(seg + 8);
    SHM_U32(s_shm, DB_PERIOD) = period;
    SHM_U32(s_shm, DB_CRC)    = db_crc(s_shm);
    /* ★★ 关键：**不自己判合法性**, 而是把"提交"这件事交给**上传路径用的同一个函数**。
     *   于是段里的非法条目会以 `DB_REJECT` 如实报出来（闸5 的同一条纪律：装载与上传共用一份校验）。*/
    SHM_U32(s_shm, DB_REQ_SEQ) = SHM_U32(s_shm, DB_REQ_SEQ) + 1u;
    dev_bind_submit();
    if (SHM_U32(s_shm, DB_REJECT) != DB_RC_OK) {
        g_db_load_bad_n++;
        SHM_U32(s_shm, DB_LOAD_BAD_N) = g_db_load_bad_n;
        return DB_SEG_BAD;
    }
    g_db_load_ok_n++;
    SHM_U32(s_shm, DB_LOAD_OK_N) = g_db_load_ok_n;
    return DB_SEG_OK;
}
