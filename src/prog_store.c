/* prog_store.c — DCL 程序持久化 (SD 卡 A/B 双副本) — 2026-09-15 (S4)
 * 契约: docs/REF-program-contract.md §6/§7。头文件里有完整设计说明。
 *
 * ★★★ 三条纪律 (改这个文件前先读):
 *   ① **CRC32 用 persist.h 导出的 `dcl_crc32`** —— 不许在本文件另写一份
 *      (本项目"同一个语义两处存放 ⇒ 只改一处就静默失效"的老族)。
 *   ② **静态校验用上层传入的 `prog_validate`** —— 不许在本文件重写一遍
 *      (契约 §7 第 5 道闸的全部意义就是"装载时与上传时是**同一套**校验")。
 *   ③ **头最后写** —— 载荷写一半掉电时, 旧头还在 ⇒ 那一份仍是"旧的有效副本",
 *      另一份始终可用。这是 A/B 方案能抗掉电的结构性原因, 不是 CRC 的运气。
 */
#include "prog_store.h"
#include "sd.h"
#include "persist.h"     /* dcl_crc32 (单一来源) */
#include "transport.h"   /* DCL_FW_VERSION_H723 */
#include <stddef.h>
#include <string.h>

volatile uint32_t g_prog_ok_n = 0, g_prog_fail_n = 0, g_prog_reject_n = 0;
volatile uint32_t g_prog_last_rc = PROG_RC_OK, g_prog_loaded_n = 0, g_prog_active_copy = 0xFFFFFFFFu;
volatile uint32_t g_prog_boot_reject = 0;
const char *volatile g_prog_reject_str = 0;

/* ★★★ 2026-09-16 **根因修复 #2b: AXI 上到底哪块是空的 —— 必须按真实地图算, 不能按注释猜。**
 *
 * 前情: 缓冲放 AXI 是对的（IDMA 读不到 DTCM），但我第一次挑的地址 **选错了**：
 *   我读 `blackbox.h` 的**过期注释**（"黑匣子区 = 0x24004000 起 **128KB**"、"AXI 完全空闲"）
 *   以为 `0x24024000` 之后空着 112KB。**错。**
 *   实情（`blackbox.h:52` 的真值）: `BB_SLOTS = 960` ⇒ 环 = 960×256B = **240KB**
 *   ⇒ 环从 `0x24004000` 一直铺到 **`0x24040000`**，**把我放缓冲的那段全占了**。
 *   症状: 读回来的载荷头是 `444c424b` = `BBLOG_REC_MAGIC 0x4B424C44`("DLBK")
 *   ⇒ **每拍被黑匣子记录覆写** ⇒ `nr` 读成 19524 ⇒ stage 数出 0 条 ACTIVE。
 *
 * ★★★ **AXI 真实地图（320KB @0x24000000..0x24050000，已用满）**:
 *       0x24000000 .. 0x24000600   引擎诊断结构固定区
 *                                  (`SD_DIAG` 0x24000200 / `BB_DIAG` 0x24000300 /
 *                                   `SD_CFG` 0x24000400 / `BOOT_AXI` 0x24000500;
 *                                   协议 `0x20 READ` 的放行窗口 = 0x24000000..0x24000600)
 *       0x24000600 .. 0x24003000   **← 唯一可用的空档 (约 10.5KB)**
 *       0x24003000 / 0x24003100    MDMA 锁存快照 / 链表节点 (`do.c`)
 *       0x24004000 .. 0x24040000   黑匣子环 (960 槽 × 256B = **240KB**)
 *       0x24040000 .. 0x24050000   日志冻结区 `SD_STAGE` (64KB)
 *   ⇒ 512B 栈式的"往大地址找空地"在这个平台上**没有空地可找**, 只能填这个 10.5KB 的缝。
 *
 * ★★ 并加**构建期硬判据**: 地址一旦越出 `0x24000600..0x24003000` 就**编译不过**。 */
#define PROG_PAY_ADDR   0x24000800u    /* 程序载荷缓冲 7680B (0x24000800..0x24002600) */
#define PROG_BLK_ADDR   0x24002800u    /* 单块读写缓冲 512B (0x24002800..0x24002A00) */
#if (PROG_PAY_ADDR < 0x24000600u) || (PROG_PAY_ADDR + 0x2000u > 0x24003000u) || \
    (PROG_BLK_ADDR < 0x24000600u) || (PROG_BLK_ADDR + 0x200u > 0x24003000u)
#error "程序存储缓冲越出 AXI 唯一空档 0x24000600..0x24003000 —— 见 prog_store.c 的 AXI 地图"
#endif

static uint8_t *const s_pay = (uint8_t *)PROG_PAY_ADDR;
static uint8_t *const s_blk = (uint8_t *)PROG_BLK_ADDR;

uint8_t *prog_store_buf(void) { return s_pay; }
uint32_t prog_store_buf_sz(void) { return (uint32_t)PROG_PAYLOAD_MAX; }   /* ★ 不能用 sizeof(s_pay): 现在 s_pay 是指针 */

/* ★ 前向声明: 这两个 + `info_scan` 的**定义**在下面 (紧邻 prog_store_probe),
 *   而 `prog_store_save` 在本文件里排得更前 —— 没有这两行就是 "used before declared"。 */
static ProgStoreInfo_t s_info;
static uint8_t s_info_ok;
static void info_scan(ProgStoreInfo_t *o);

/* ── 内部: 读并校验第 idx 份的头 (idx: 0=A, 1=B)。1 = 头有效 ── */
static int copy_hdr(int idx, ProgCopyHdr_t *h)
{
    uint32_t base = idx ? g_sd_prog_b : g_sd_prog_a;
    if (!g_sd_part_ok) return 0;
    if (sd_read_block(base, s_blk) != 0) return 0;
    memcpy(h, s_blk, sizeof(*h));
    if (h->magic != PROG_HDR_MAGIC || h->version != PROG_HDR_VERSION) return 0;
    /* ★ 一个合法程序载荷至少 6 字节 (counts 头)。用 `len == 0` 判会把"6 字节残片"放进来。 */
    if (h->len < 6u || h->len > PROG_PAYLOAD_MAX) return 0;
    /* 头自身校验和: 覆盖 hdr_sum **之前**的全部字 */
    uint32_t nw = (uint32_t)(offsetof(ProgCopyHdr_t, hdr_sum) / 4u);
    uint32_t sum = 0u;
    const uint32_t *w = (const uint32_t *)h;
    for (uint32_t i = 0; i < nw; i++) sum += w[i];
    if (sum != h->hdr_sum) return 0;
    return 1;
}

/* ── 内部: 从卡上读第 idx 份的载荷到 out, 并对**读回的字节**算 CRC32。
 *   1 = 有效。★ `out` 必须非空 —— 本模块不提供"只验 CRC 不取数据"的路径:
 *   那种路径最容易退化成"信头里的值", 而契约要的是"用实测字节校验"。 ── */
static int copy_payload(int idx, const ProgCopyHdr_t *h, uint8_t *out, uint32_t cap)
{
    uint32_t base = idx ? g_sd_prog_b : g_sd_prog_a;
    uint32_t off = 0u, nblk = (h->len + SD_BLK_SZ - 1u) / SD_BLK_SZ;
    if (out == 0 || cap < h->len) return 0;
    for (uint32_t b = 0; b < nblk; b++) {
        if (sd_read_block(base + 1u + b, s_blk) != 0) return 0;
        uint32_t take = h->len - off;
        if (take > SD_BLK_SZ) take = SD_BLK_SZ;
        memcpy(out + off, s_blk, take);
        off += take;
    }
    /* ★ CRC 对**实际读回的字节**算, 而不是信头里的 len/crc */
    return (dcl_crc32(out, h->len) == h->crc32) ? 1 : 0;
}

/* ── 覆盖写 ── */
int prog_store_save(const uint8_t *payload, uint32_t len, const ProgManifest_t *mf)
{
    ProgCopyHdr_t ha, hb, nh;
    int va, vb, target;
    uint32_t seq_max = 0u, new_seq, base, nblk;

    if (!g_sd_part_ok) { g_prog_last_rc = PROG_RC_NOPART; g_prog_fail_n++; return PROG_RC_NOPART; }
    if (payload == 0 || len == 0u || len > PROG_PAYLOAD_MAX) {
        g_prog_last_rc = PROG_RC_LEN; g_prog_fail_n++; return PROG_RC_LEN;
    }
    va = copy_hdr(0, &ha);
    vb = copy_hdr(1, &hb);
    if (va) seq_max = ha.seq;
    if (vb && hb.seq > seq_max) seq_max = hb.seq;
    /* ★ 目标 = 无效的那份优先; 两份都有效就选 seq **较旧**的 (留着那份好的做后盾) */
    if (!va)      target = 0;
    else if (!vb) target = 1;
    else          target = (ha.seq <= hb.seq) ? 0 : 1;
    new_seq = seq_max + 1u;
    base    = target ? g_sd_prog_b : g_sd_prog_a;

    /* ⓪ 先把载荷规整进 s_pay 并补零尾块 —— ★ 必须在写之前, 不能漏 (上一版这里被误删过) */
    nblk = (len + SD_BLK_SZ - 1u) / SD_BLK_SZ;
    /* ★ 上传事务与落盘共用同一个缓冲 ⇒ 允许"源就是 s_pay" (自拷是 UB, 跳过即可) */
    if (payload != s_pay) memcpy(s_pay, payload, len);
    if (nblk * SD_BLK_SZ > len) memset(s_pay + len, 0, nblk * SD_BLK_SZ - len);  /* 尾块补零 */

    /* ① 载荷 (从第 1 块起) —— ★ **逐块用 CMD24 单块写**，不走 CMD23/CMD25 多块路径。
     *   理由(实测): 多块写在本项目里**只被日志以 128 块的大批量用过**；
     *   "1 块的多块写"是**未验证路径**，实测 COMMIT 会卡在那里(板子还活着但 handler 不返回)。
     *   ★ 单块写是 HAL 里最简单、最直的那条路(`HAL_SD_WriteBlocks` 的 NumberOfBlocks==1 分支),
     *     程序存储是**低频**操作(一次落盘十几块)，不值得为省几次命令去冒未验证路径的风险。 */
    for (uint32_t b = 0; b < nblk; b++) {
        if (sd_write_block(base + 1u + b, s_pay + b * SD_BLK_SZ) != 0) {
            g_prog_last_rc = PROG_RC_WRITE; g_prog_fail_n++;
            g_prog_reject_str = "payload write failed";
            return PROG_RC_WRITE;
        }
    }

    /* ② ★闸3: 回读逐块比对。不符 ⇒ **不写头** ⇒ 这一份仍是"无效/旧有效", 另一份不受影响 */
    for (uint32_t b = 0; b < nblk; b++) {
        uint32_t take = len - b * SD_BLK_SZ;
        if (take > SD_BLK_SZ) take = SD_BLK_SZ;
        if (sd_read_block(base + 1u + b, s_blk) != 0 ||
            memcmp(s_blk, s_pay + b * SD_BLK_SZ, take) != 0) {
            g_prog_last_rc = PROG_RC_VERIFY; g_prog_fail_n++;
            g_prog_reject_str = "payload readback mismatch";
            return PROG_RC_VERIFY;
        }
    }

    /* ③ ★ 头**最后**写 (原子性: 头在 = 这份才算数) */
    memset(&nh, 0, sizeof(nh));
    nh.magic    = PROG_HDR_MAGIC;
    nh.version  = PROG_HDR_VERSION;
    nh.seq      = new_seq;
    nh.crc32    = dcl_crc32(payload, len);
    nh.len      = len;
    nh.prog_id  = mf ? mf->prog_id  : 0u;
    nh.prog_ver = mf ? mf->prog_ver : 0u;
    nh.min_fw   = mf ? mf->min_fw   : 0u;
    nh.req_caps = mf ? mf->req_caps : 0u;
    nh.fw_ver   = (uint32_t)DCL_FW_VERSION_H723;
    if (len >= 6u) {
        nh.n_routes = (uint16_t)(payload[0] | ((uint16_t)payload[1] << 8));
        nh.n_params = (uint16_t)(payload[2] | ((uint16_t)payload[3] << 8));
        nh.n_states = (uint16_t)(payload[4] | ((uint16_t)payload[5] << 8));
    }
    {   /* 头校验和: 覆盖 hdr_sum 之前的全部字 */
        uint32_t nw = (uint32_t)(offsetof(ProgCopyHdr_t, hdr_sum) / 4u);
        uint32_t sum = 0u;
        const uint32_t *w = (const uint32_t *)&nh;
        for (uint32_t i = 0; i < nw; i++) sum += w[i];
        nh.hdr_sum = sum;
    }
    memset(s_blk, 0, SD_BLK_SZ);
    memcpy(s_blk, &nh, sizeof(nh));
    if (sd_write_block(base, s_blk) != 0) {
        g_prog_last_rc = PROG_RC_WRITE; g_prog_fail_n++; return PROG_RC_WRITE;
    }
    /* ④ 回读头确认 (写了就算? 不行 —— 必须能读回来才算) */
    { ProgCopyHdr_t chk; if (!copy_hdr(target, &chk) || chk.seq != new_seq) {
          g_prog_last_rc = PROG_RC_VERIFY; g_prog_fail_n++; return PROG_RC_VERIFY; } }

    g_prog_ok_n++;
    g_prog_last_rc = PROG_RC_OK;
    g_prog_active_copy = (uint32_t)target;
    /* ★ 就地更新缓存 —— **不再回读 SD**(我们刚写下的东西就是权威)。
     *   回读会让"保存"变成又一轮 30 块读 ⇒ 又一次触发停滞判据。 */
    s_info_ok = 1u;
    if (target == 0) { s_info.seq_a = new_seq; s_info.len_a = len; s_info.crc_a = nh.crc32; }
    else             { s_info.seq_b = new_seq; s_info.len_b = len; s_info.crc_b = nh.crc32; }
    s_info.part_ok  = g_sd_part_ok;
    s_info.active   = (uint32_t)target;
    s_info.ab_valid = (uint32_t)((s_info.crc_a ? 1u : 0u) | (s_info.crc_b ? 2u : 0u));
    return PROG_RC_OK;
}

/* ── 读"有效且 seq 最大"的那一份 ── */
int prog_store_load(uint8_t *out, uint32_t cap, ProgManifest_t *mf_out)
{
    ProgCopyHdr_t ha, hb;
    int va, vb, pick = -1;
    if (!g_sd_part_ok) { g_prog_last_rc = PROG_RC_NOPART; return PROG_RC_NOPART; }
    va = copy_hdr(0, &ha);
    vb = copy_hdr(1, &hb);
    if (va && copy_payload(0, &ha, out, cap)) pick = 0;
    if (vb && copy_payload(1, &hb, out, cap)) {
        if (pick < 0 || hb.seq > ha.seq) pick = 1;
    }
    if (pick < 0) { g_prog_last_rc = PROG_RC_NONE; return PROG_RC_NONE; }
    if (mf_out) {
        const ProgCopyHdr_t *h = pick ? &hb : &ha;
        mf_out->prog_id  = h->prog_id;
        mf_out->prog_ver = h->prog_ver;
        mf_out->min_fw   = h->min_fw;
        mf_out->req_caps = h->req_caps;
    }
    g_prog_active_copy = (uint32_t)pick;
    g_prog_last_rc = PROG_RC_OK;
    return PROG_RC_OK;
}

int prog_store_erase(void)
{
    int rc = PROG_RC_OK;
    if (!g_sd_part_ok) return PROG_RC_NOPART;
    memset(s_blk, 0, SD_BLK_SZ);                 /* 头全 0 ⇒ magic 不符 ⇒ 无效 */
    if (sd_write_block(g_sd_prog_a, s_blk) != 0) rc = PROG_RC_WRITE;
    if (sd_write_block(g_sd_prog_b, s_blk) != 0) rc = PROG_RC_WRITE;
    g_prog_active_copy = 0xFFFFFFFFu;
    /* ★ 缓存就地清零 (不回读) */
    s_info_ok = 1u;
    s_info.seq_a = s_info.seq_b = 0u;
    s_info.crc_a = s_info.crc_b = 0u;
    s_info.len_a = s_info.len_b = 0u;
    s_info.ab_valid = 0u;
    s_info.active = 0xFFFFFFFFu;
    return rc;
}

/* ★★★ 2026-09-15 修（实测事故）: **体检绝不读 SD —— 除开机那一次。**
 *
 * 起因: 第一次调用 `0x48`(PROG_STATUS) 之后板子进复位循环。SWD 取证:
 *   `g_loop_entered=1` 而 `g_cmd_count=0`(命令从未被分发) ⇒ **主循环停在第一轮里**;
 *   项目自带的停滞阈值是 12000 拍 = **1.2 s**, 超了走 `eng_outputs_safe()` + 复位。
 * 根因: 原实现会 `copy_hdr`+`copy_payload` —— **最多连续读 30 个 SD 块**。
 *   ① 它让 `prog_store` 成了 SD 的**第二个访问者** ⇒ 违反契约 §9.5「单写者」红线;
 *   ② 一次被卡住就超过 1.2 s 停滞阈值 ⇒ 被判定"主循环死了" ⇒ 复位。
 *
 * ⇒ 处置: 体检数据**只在开机扫描一次**并缓存; 查询口只回缓存, **零 SD 访问**。
 *   ★ 这是"观测不得扰动被测对象"那条纪律在存储域的应用。
 */
static ProgStoreInfo_t s_info;      /* (前向声明在文件上方) */
static uint8_t s_info_ok = 0u;

/* ★ 唯一会读 SD 的体检路径 —— **只允许在开机时调用** (那时日志还没开始轮询)。 */
static void info_scan(ProgStoreInfo_t *o)
{
    ProgCopyHdr_t ha, hb;
    memset(o, 0, sizeof(*o));
    o->part_ok = g_sd_part_ok;
    o->active  = 0xFFFFFFFFu;
    if (!g_sd_part_ok) return;
    int va = copy_hdr(0, &ha);          /* 读 1 块 */
    int vb = copy_hdr(1, &hb);          /* 读 1 块 */
    if (va) { o->seq_a = ha.seq; o->len_a = ha.len; }
    if (vb) { o->seq_b = hb.seq; o->len_b = hb.len; }
    /* ★ 头里的 CRC 只作"这份头自称有效"的标记; 真正的载荷核验在 boot_load/load 里做,
     *   这里**不再重复读 30 块** —— 那正是本次事故的动作。 */
    o->crc_a = va ? ha.crc32 : 0u;
    o->crc_b = vb ? hb.crc32 : 0u;
    o->ab_valid = (uint32_t)((va ? 1u : 0u) | (vb ? 2u : 0u));
    if (o->crc_a && (!o->crc_b || ha.seq >= hb.seq)) o->active = 0u;
    else if (o->crc_b)                               o->active = 1u;
}

void prog_store_probe(ProgStoreInfo_t *o)
{
    if (!o) return;
    if (!s_info_ok) { info_scan(&s_info); s_info_ok = 1u; }   /* 兜底: 没扫过才扫一次 */
    /* 计数类每问一次就刷新 —— 它们只住在 RAM, 不碰 SD */
    s_info.part_ok     = g_sd_part_ok;
    s_info.ok_n        = g_prog_ok_n;
    s_info.fail_n      = g_prog_fail_n;
    s_info.reject_n    = g_prog_reject_n;
    s_info.last_rc     = g_prog_last_rc;
    s_info.loaded_n    = g_prog_loaded_n;
    s_info.boot_reject = g_prog_boot_reject;
    *o = s_info;
}

/* ── 开机装载: 读 → **闸5 同一套静态校验** → 交给上层部署 ──
 * ★ 这是契约里最容易被漏掉的一条: 程序在卡上躺了几个月, 可能写坏、位翻转,
 *   或者**换了一个不同版本的固件**。所以**装载时必须重跑与上传时完全相同的校验**,
 *   不过就**不装载** (保持旧程序运行), 绝不能"装进去让它跑出奇怪行为"。
 *   而"同一套"的实现方式 = 调用方把 `prog_validate` 传进来 —— 这里不重写一份。 */
int prog_store_boot_load(uint8_t *scratch, uint32_t cap, prog_validate_fn vfn)
{
    ProgManifest_t mf;
    uint32_t n, budget = 0u;
    int rc;

    if (!g_sd_part_ok) return PROG_RC_NOPART;
    /* ★★ 先做**唯一一次**体检扫描 (只读 2 个头块), 顺便把缓存置为有效 ——
     *   之后 `prog_store_probe` 就再也不会碰 SD 了 (那是本次事故的动作)。 */
    info_scan(&s_info);
    s_info_ok = 1u;
    rc = prog_store_load(scratch, cap, &mf);
    if (rc != PROG_RC_OK) return rc;

    /* 载荷长度由它自述的 counts 推出 (与 prog_validate 的算法一致) */
    n = 6u + ((uint32_t)(scratch[0] | ((uint16_t)scratch[1] << 8))
            + (uint32_t)(scratch[2] | ((uint16_t)scratch[3] << 8))
            + (uint32_t)(scratch[4] | ((uint16_t)scratch[5] << 8))) * 16u;

    /* R3 版本门: 程序要求的固件版本高于本机 ⇒ 拒绝 (不是"试试看") */
    if (mf.min_fw != 0u && (uint32_t)mf.min_fw > (uint32_t)DCL_FW_VERSION_H723) {
        g_prog_boot_reject = 1u;
        g_prog_reject_str  = "min_fw too high";
        g_prog_last_rc     = PROG_RC_MINF;
        g_prog_reject_n++;
        return PROG_RC_MINF;
    }
    /* ★闸5: 重跑**上传时那一套**校验 (同一个函数指针, 不是第二份实现) */
    if (vfn) {
        const char *err = vfn(scratch, n, &budget);
        if (err) {
            g_prog_boot_reject = 1u;
            g_prog_reject_str  = err;      /* 指向二进制里的静态串, 供调试读 */
            g_prog_last_rc     = PROG_RC_VALIDATE;
            g_prog_reject_n++;
            return PROG_RC_VALIDATE;
        }
    }
    g_prog_boot_reject = 0u;
    g_prog_loaded_n    = n;
    return PROG_RC_OK;
}
