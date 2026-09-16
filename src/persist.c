/**
 * persist.c — 配置掉电保持实现 (裸 Flash 双副本 A/B)
 *
 * 设计要点见 persist.h。这里只补充**实现层面的关键决策**:
 *
 * ① **读写都走 32B 对齐的 RAM 缓冲**: H7 必须先擦后写、且写粒度是 32B flash word,
 *    所以 payload 长度必须先补齐到 32B 边界 (补 0xFF —— 擦除态, 按位与会保留原值)。
 *    直接从 SHM 表区写会**越界写到表尾之后的字节**, 所以一律先拷进本地缓冲。
 *
 * ② **回读校验是"写成功"的唯一判据**: 硬件不保证"命令返回就写对了"。
 *    写完立刻把整段读回来和 RAM 缓冲逐字节比对 —— 这才叫"落盘成功"。
 *    注意: 回读用**普通内存读** (0x080xxxxx 可直接读), 不走 flash 控制器。
 *
 * ③ **"写旧的那份"而不是轮流写**: 见 persist.h 的对偶性说明。
 *
 * ④ **不在 ISR 里**: 全程阻塞自旋等 QW。擦除 1~4 秒, 只能主循环调用。
 */
#include "persist.h"
#include "flash.h"
#include "engine.h"
#include "transport.h"   /* crc32? 没有 —— 用本地实现, 见下 */
#include "wdt.h"         /* ★ wdt_set_timeout_ms —— 落盘窗口 (见下方 wdt_window_*) */

/* ══════════════════ CRC32 (IEEE 802.3, poly 0xEDB88320 反射式) ══════════════════
 * ★ 为什么用 CRC32 而不是 S3 的 CRC16-CCITT: persist 载荷是 6KB 级 (S3 是 6KB 也用了
 *   CRC16, 但那里还有 NVS 自己的校验兜底)。裸扇区没有第二道防线, 6KB 上 CRC16
 *   的漏检率 (1/65536) 对"配置表"偏低。CRC32 是本项目现成的算法 (transmsport 帧
 *   用的是 CRC16, 但 AXI/以太网都用 CRC32), 代价可以忽略。
 * ★ 表驱动实现 (256 × u32 = 1KB ROM), 比逐位快得多且代码更短。
 * ★ 必须与 Python 侧 `zlib.crc32()` 逐位一致 —— 工具的判据就是这么算的。 */
static const uint32_t k_crc32_tab[256] = {
    0x00000000u,0x77073096u,0xEE0E612Cu,0x990951BAu,0x076DC419u,0x706AF48Fu,0xE963A535u,0x9E6495A3u,
    0x0EDB8832u,0x79DCB8A4u,0xE0D5E91Eu,0x97D2D988u,0x09B64C2Bu,0x7EB17CBDu,0xE7B82D07u,0x90BF1D91u,
    0x1DB71064u,0x6AB020F2u,0xF3B97148u,0x84BE41DEu,0x1ADAD47Du,0x6DDDE4EBu,0xF4D4B551u,0x83D385C7u,
    0x136C9856u,0x646BA8C0u,0xFD62F97Au,0x8A65C9ECu,0x14015C4Fu,0x63066CD9u,0xFA0F3D63u,0x8D080DF5u,
    0x3B6E20C8u,0x4C69105Eu,0xD56041E4u,0xA2677172u,0x3C03E4D1u,0x4B04D447u,0xD20D85FDu,0xA50AB56Bu,
    0x35B5A8FAu,0x42B2986Cu,0xDBBBC9D6u,0xACBCF940u,0x32D86CE3u,0x45DF5C75u,0xDCD60DCFu,0xABD13D59u,
    0x26D930ACu,0x51DE003Au,0xC8D75180u,0xBFD06116u,0x21B4F4B5u,0x56B3C423u,0xCFBA9599u,0xB8BDA50Fu,
    0x2802B89Eu,0x5F058808u,0xC60CD9B2u,0xB10BE924u,0x2F6F7C87u,0x58684C11u,0xC1611DABu,0xB6662D3Du,
    0x76DC4190u,0x01DB7106u,0x98D220BCu,0xEFD5102Au,0x71B18589u,0x06B6B51Fu,0x9FBFE4A5u,0xE8B8D433u,
    0x7807C9A2u,0x0F00F934u,0x9609A88Eu,0xE10E9818u,0x7F6A0DBBu,0x086D3D2Du,0x91646C97u,0xE6635C01u,
    0x6B6B51F4u,0x1C6C6162u,0x856530D8u,0xF262004Eu,0x6C0695EDu,0x1B01A57Bu,0x8208F4C1u,0xF50FC457u,
    0x65B0D9C6u,0x12B7E950u,0x8BBEB8EAu,0xFCB9887Cu,0x62DD1DDFu,0x15DA2D49u,0x8CD37CF3u,0xFBD44C65u,
    0x4DB26158u,0x3AB551CEu,0xA3BC0074u,0xD4BB30E2u,0x4ADFA541u,0x3DD895D7u,0xA4D1C46Du,0xD3D6F4FBu,
    0x4369E96Au,0x346ED9FCu,0xAD678846u,0xDA60B8D0u,0x44042D73u,0x33031DE5u,0xAA0A4C5Fu,0xDD0D7CC9u,
    0x5005713Cu,0x270241AAu,0xBE0B1010u,0xC90C2086u,0x5768B525u,0x206F85B3u,0xB966D409u,0xCE61E49Fu,
    0x5EDEF90Eu,0x29D9C998u,0xB0D09822u,0xC7D7A8B4u,0x59B33D17u,0x2EB40D81u,0xB7BD5C3Bu,0xC0BA6CADu,
    0xEDB88320u,0x9ABFB3B6u,0x03B6E20Cu,0x74B1D29Au,0xEAD54739u,0x9DD277AFu,0x04DB2615u,0x73DC1683u,
    0xE3630B12u,0x94643B84u,0x0D6D6A3Eu,0x7A6A5AA8u,0xE40ECF0Bu,0x9309FF9Du,0x0A00AE27u,0x7D079EB1u,
    0xF00F9344u,0x8708A3D2u,0x1E01F268u,0x6906C2FEu,0xF762575Du,0x806567CBu,0x196C3671u,0x6E6B06E7u,
    0xFED41B76u,0x89D32BE0u,0x10DA7A5Au,0x67DD4ACCu,0xF9B9DF6Fu,0x8EBEEFF9u,0x17B7BE43u,0x60B08ED5u,
    0xD6D6A3E8u,0xA1D1937Eu,0x38D8C2C4u,0x4FDFF252u,0xD1BB67F1u,0xA6BC5767u,0x3FB506DDu,0x48B2364Bu,
    0xD80D2BDAu,0xAF0A1B4Cu,0x36034AF6u,0x41047A60u,0xDF60EFC3u,0xA867DF55u,0x316E8EEFu,0x4669BE79u,
    0xCB61B38Cu,0xBC66831Au,0x256FD2A0u,0x5268E236u,0xCC0C7795u,0xBB0B4703u,0x220216B9u,0x5505262Fu,
    0xC5BA3BBEu,0xB2BD0B28u,0x2BB45A92u,0x5CB36A04u,0xC2D7FFA7u,0xB5D0CF31u,0x2CD99E8Bu,0x5BDEAE1Du,
    0x9B64C2B0u,0xEC63F226u,0x756AA39Cu,0x026D930Au,0x9C0906A9u,0xEB0E363Fu,0x72076785u,0x05005713u,
    0x95BF4A82u,0xE2B87A14u,0x7BB12BAEu,0x0CB61B38u,0x92D28E9Bu,0xE5D5BE0Du,0x7CDCEFB7u,0x0BDBDF21u,
    0x86D3D2D4u,0xF1D4E242u,0x68DDB3F8u,0x1FDA836Eu,0x81BE16CDu,0xF6B9265Bu,0x6FB077E1u,0x18B74777u,
    0x88085AE6u,0xFF0F6A70u,0x66063BCAu,0x11010B5Cu,0x8F659EFFu,0xF862AE69u,0x616BFFD3u,0x166CCF45u,
    0xA00AE278u,0xD70DD2EEu,0x4E048354u,0x3903B3C2u,0xA7672661u,0xD06016F7u,0x4969474Du,0x3E6E77DBu,
    0xAED16A4Au,0xD9D65ADCu,0x40DF0B66u,0x37D83BF0u,0xA9BCAE53u,0xDEBB9EC5u,0x47B2CF7Fu,0x30B5FFE9u,
    0xBDBDF21Cu,0xCABAC28Au,0x53B39330u,0x24B4A3A6u,0xBAD03605u,0xCDD70693u,0x54DE5729u,0x23D967BFu,
    0xB3667A2Eu,0xC4614AB8u,0x5D681B02u,0x2A6F2B94u,0xB40BBE37u,0xC30C8EA1u,0x5A05DF1Bu,0x2D02EF8Du
};

uint32_t dcl_crc32(const uint8_t *d, size_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) c = k_crc32_tab[(c ^ d[i]) & 0xFFu] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

/* ══════════════════ 观测面 ══════════════════ */
volatile uint32_t g_persist_save_ok    = 0;
volatile uint32_t g_persist_save_fail  = 0;
volatile uint32_t g_persist_load_ok    = 0;
volatile uint32_t g_persist_load_fail  = 0;
volatile uint32_t g_persist_load_seq   = 0;
volatile uint32_t g_persist_last_err   = 0;
volatile uint32_t g_persist_writes     = 0;
volatile uint32_t g_persist_dirty      = 0;
volatile uint32_t g_persist_target     = 0xFFFFFFFFu;
volatile uint32_t g_persist_erase_ok   = 0;
volatile uint32_t g_persist_erase_fail = 0;

/* ══════════════════ 内部缓冲 ══════════════════
 * 6KB payload + 32B header, 补齐到 32B 倍数。静态区 (DTCM), 不占栈。
 * ★ 单独一块而不是复用 SHM: 避免"落盘期间 SHM 被协议改动"导致写出的字节
 *   与 header 里的条数/CRC 不一致 (那种不一致在回读校验时才会暴露, 但那时
 *   已经写进 flash 了)。 */
#define PERSIST_PAYLOAD_MAX  ((MAX_ROUTES + MAX_PARAMS + MAX_STATES) * 16u)   /* 6144 */
#define PERSIST_BLOB_MAX     (((PERSIST_HDR_SIZE + PERSIST_PAYLOAD_MAX + 31u) / 32u) * 32u)

static uint8_t s_blob[PERSIST_BLOB_MAX] __attribute__((aligned(32)));
static uint8_t s_rdbuf[PERSIST_BLOB_MAX] __attribute__((aligned(32)));
/* ★ 落盘快照: save 时把 ACTIVE 表拷进这里, 使"擦除(1~4秒)期间 SHM 被改"
 *   不影响正在写的这份数据的自洽性。 */
static uint8_t s_snap[PERSIST_PAYLOAD_MAX] __attribute__((aligned(32)));

_Static_assert(sizeof(s_blob) % 32u == 0u, "persist blob must be a whole number of flash words");

/* ══════════════════ 副本读写原语 ══════════════════ */

/** @brief 读一份副本的 header (纯内存读, 不走 flash 控制器) */
static void read_hdr(uint32_t sector, PersistHdr_t *h)
{
    const uint8_t *p = (const uint8_t *)(uintptr_t)flash_sector_base(sector);
    __builtin_memcpy(h, p, sizeof(PersistHdr_t));
}

/** @brief 判一份副本是否有效 (magic + version + 条数 + CRC), 返回 1/0
 *  @param h    已读出的 header
 *  @param sector 该 header 所在扇区 (用于算 payload 地址)
 *  @param crc_out 可选: 有效时输出实测 CRC (供工具体检) */
static int copy_valid(const PersistHdr_t *h, uint32_t sector, uint32_t *crc_out)
{
    if (h->magic   != PERSIST_MAGIC)   return 0;
    if (h->version != PERSIST_VERSION) return 0;
    if (h->n_routes > MAX_ROUTES) return 0;
    if (h->n_params > MAX_PARAMS) return 0;
    if (h->n_states > MAX_STATES) return 0;
    uint32_t plen = ((uint32_t)h->n_routes + h->n_params + h->n_states) * 16u;
    if (plen > PERSIST_PAYLOAD_MAX) return 0;
    const uint8_t *p = (const uint8_t *)(uintptr_t)(flash_sector_base(sector) + PERSIST_HDR_SIZE);
    uint32_t c = dcl_crc32(p, plen);
    if (crc_out) *crc_out = c;
    return (c == h->crc32) ? 1 : 0;
}

int persist_probe(PersistInfo_t *out)
{
    PersistHdr_t ha, hb;
    read_hdr(PERSIST_SECTOR_A, &ha);
    read_hdr(PERSIST_SECTOR_B, &hb);
    uint32_t ca = 0, cb = 0;
    int va = copy_valid(&ha, PERSIST_SECTOR_A, &ca);
    int vb = copy_valid(&hb, PERSIST_SECTOR_B, &cb);

    out->ab_valid = (uint8_t)((va ? PERSIST_AB_A : 0u) | (vb ? PERSIST_AB_B : 0u));
    out->seq_a = va ? ha.seq : 0u;
    out->seq_b = vb ? hb.seq : 0u;
    out->crc_a = va ? ca : 0u;
    out->crc_b = vb ? cb : 0u;

    /* 决定"接下来该写哪份": 有效的两份里 seq 小的那份 (它被替换掉不损失任何东西);
     * 只有一份有效 → 写另一份; 都无效 → 写 A 并让 seq 从 1 开始。
     * ★★ M3 修复 (2026-09-11 外部审计): `n_routes/n_params/n_states` 必须取**最新有效
     *    副本**(seq 大)的那份 —— 0x43 问的是"flash 里持久化了几条"。
     *    旧实现两份都有效时固定取 A ⇒ **两份条数不同时报错**。
     *    复现: RESET → deploy 3 → 落盘 → deploy 8 → 落盘 ⇒ 旧代码报 3, 真值 8。
     *    (这与 `active` 的语义**不是一回事**: active 是"待覆盖的旧副本"。别混用。) */
    if (va && vb) {
        const PersistHdr_t *hi = (ha.seq >= hb.seq) ? &ha : &hb;   /* 最新有效副本 */
        const PersistHdr_t *lo = (ha.seq >= hb.seq) ? &hb : &ha;
        out->active = (ha.seq <= hb.seq) ? 0u : 1u;               /* 待覆盖(旧的)那份 */
        out->n_routes = hi->n_routes; out->n_params = hi->n_params; out->n_states = hi->n_states;
        out->n_routes_old = lo->n_routes;
    } else if (va) {
        out->active = 1u;                             /* 写 B (空的那份) */
        out->n_routes = ha.n_routes; out->n_params = ha.n_params; out->n_states = ha.n_states;
        out->n_routes_old = 0u;
    } else if (vb) {
        out->active = 0u;
        out->n_routes = hb.n_routes; out->n_params = hb.n_params; out->n_states = hb.n_states;
        out->n_routes_old = 0u;
    } else {
        out->active = 0u;
        out->n_routes = 0u; out->n_params = 0u; out->n_states = 0u;
        out->n_routes_old = 0u;
    }
    return 0;
}

const char *persist_err_str(int r)
{
    switch (r) {
        case FL_OK:              return "ok";
        case FL_ERR_TIMEOUT:     return "flash timeout";
        case FL_ERR_LOCKED:      return "flash locked";
        case FL_ERR_SRERROR:     return "flash sr error";
        case FL_ERR_ALIGN:       return "flash align";
        case FL_ERR_RANGE:       return "flash range";
        case FL_ERR_NOSECTOR:    return "flash no sector";
        default:                 return "persist err";
    }
}

/* ══════════════════ 加载 ══════════════════ */

/** @brief 把某份副本的 payload 拷进 ACTIVE 表 + 归组重建桶
 *  ★ 只写"部署组态", **绝不置 ENGINE_RUN** (安全语义: 上电不自动运行)。 */
static int load_from(uint32_t sector, const PersistHdr_t *h)
{
    const uint8_t *p = (const uint8_t *)(uintptr_t)(flash_sector_base(sector) + PERSIST_HDR_SIZE);
    uint8_t *base = g_shm;

    /* 路由表: 副本里存的是**已归组序** (save 时从 ACTIVE 拷) → 直接落地 */
    if (h->n_routes) {
        __builtin_memcpy((void *)(base + OFF_ROUTE_TABLE), p, (size_t)h->n_routes * 16u);
        p += (size_t)h->n_routes * 16u;
    }
    if (h->n_params) {
        __builtin_memcpy((void *)(base + OFF_PARAM_TABLE), p, (size_t)h->n_params * 16u);
        p += (size_t)h->n_params * 16u;
    }
    /* ★ M2 语义: 状态表先全清再拷 —— 新上电绝不继承旧运行状态。
     *   虽然 SHM 上电已 memset, 但 RESET/deploy 后调用本函数时表里可能有残留。 */
    __builtin_memset((void *)(base + OFF_STATE_TABLE), 0, MAX_STATES * 16u);
    if (h->n_states) {
        __builtin_memcpy((void *)(base + OFF_STATE_TABLE), p, (size_t)h->n_states * 16u);
    }

    SHM_U16(base, OFF_CTRL_N_ROUTES) = h->n_routes;
    SHM_U16(base, OFF_CTRL_N_PARAMS) = h->n_params;
    SHM_U16(base, OFF_CTRL_N_STATES) = h->n_states;
    SHM_U32(base, OFF_CTRL_PROG_MAGIC) = h->prog_magic;

    /* ★ 必须重建桶表: 副本里的路由是归组序, 但**桶索引是 RAM 里的**(不落盘)。
     *   不重建 → ISR 用空桶扫不到任何路由 (S3 OA15 的连带缺陷同款)。 */
    engine_build_buckets(base, h->n_routes);
    __asm__ volatile("dsb" ::: "memory");
    return (int)(h->n_routes + h->n_params + h->n_states);
}

int persist_load(uint8_t *base)
{
    (void)base;   /* 本实现固定用 g_shm (persist 只服务这一块表区) */
    PersistInfo_t info;
    persist_probe(&info);

    PersistHdr_t ha, hb;
    read_hdr(PERSIST_SECTOR_A, &ha);
    read_hdr(PERSIST_SECTOR_B, &hb);
    int va = (info.ab_valid & PERSIST_AB_A) ? 1 : 0;
    int vb = (info.ab_valid & PERSIST_AB_B) ? 1 : 0;

    /* 选 seq 大的那份; 若只有一份有效就用它 */
    int use_a;
    if (va && vb)      use_a = (ha.seq >= hb.seq);
    else if (va)       use_a = 1;
    else if (vb)       use_a = 0;
    else {
        g_persist_load_fail++;
        return 0;      /* 无有效副本 = 空配置, 不是错误 */
    }

    const PersistHdr_t *h = use_a ? &ha : &hb;
    uint32_t sector = use_a ? PERSIST_SECTOR_A : PERSIST_SECTOR_B;
    int n = load_from(sector, h);
    if (n <= 0) { g_persist_load_fail++; return -1; }
    g_persist_load_ok++;
    g_persist_load_seq = h->seq;
    return n;
}

/* ══════════════════ 落盘 ══════════════════ */

/* ══════════ ★★★ 落盘窗口: 临时放大看门狗超时 (2026-09-13) ══════════
 * 为什么必须这么做 (实测, 别再从头查一遍):
 *   擦 flash 期间拍 ISR 的 `wdt_feed()`(写 IWDG_KR) **无法完成** ⇒ ISR 卡住不返回
 *   ⇒ 喂狗停 ⇒ 200ms 后 IWDG 复位 ⇒ "保存"变成"重启", 且配置从未落盘。
 *   ★ L1 实测: 同一份固件去掉 DCL_WDT 后落盘**完全成功**
 *     (writes=1 / nr=128 / 0.97s / 未复位) ⇒ **喂狗是唯一障碍, persist 本身没问题**。
 *   见 docs/audit/H723-PERSIST-WDT-DEFECT.md §11 / §12.2b。
 *
 * ★ 窗口值必须 ≥ `FL_ERASE_TIMEOUT_CYC`(flash.c 的擦除超时预算, 8s) —— 项目已有同款教训:
 *   "清空窗口必须 ≥ 该操作**自己的**超时预算, 否则 3s 的擦除会在 2.5s 处被停滞自愈复位"。
 *   这三个量是**同一件事的三种单位**, 改一个必须改另外两个:
 *     PERSIST_BLOCK_TICKS(main.c) · FL_ERASE_TIMEOUT_CYC(flash.c) · WDT_PERSIST_WINDOW_MS(此处)
 * ★ 正常擦除端到端仅 **0.97s**(实测); 8s 是给**失败路径**(擦除卡到超时)留的。
 * ★ 代价必须说清(不许粉饰): 窗口内是"看门狗保护真空", 最坏 8s。
 *   所以它**只覆盖 persist_save 的擦写段**, 且**无论成败都在单一出口恢复** —— 见 out:。 */
#ifndef WDT_PERSIST_WINDOW_MS
#define WDT_PERSIST_WINDOW_MS  8000u
#endif

/** 开窗: 返回旧超时(供恢复); 0 = 未改成功(调用方**不要**恢复, 保持原样)。 */
static inline uint32_t wdt_window_open(void)
{
#if defined(DCL_WDT) && DCL_WDT
    /* ★★ 0 档 = **改前行为** (不开窗) —— 项目纪律: 每个特性都要有能打出旧行为的对照,
     *   否则"新档 PASS"不构成证据 (我们无法排除"判据根本量不出差别")。
     *   对照档的预期结果就是**复位**, 见 docs/audit/H723-PERSIST-WDT-DEFECT.md §12.4。 */
    if (WDT_PERSIST_WINDOW_MS == 0u) return 0u;
    return wdt_set_timeout_ms(WDT_PERSIST_WINDOW_MS);
#else
    return 0u;                       /* 该档没有看门狗 ⇒ 无事可做 */
#endif
}

/** 关窗: prev == 0 表示当初没改成功 ⇒ 保持原样。 */
static inline void wdt_window_close(uint32_t prev)
{
#if defined(DCL_WDT) && DCL_WDT
    if (prev != 0u) (void)wdt_set_timeout_ms(prev);
#else
    (void)prev;
#endif
}

int persist_save(uint8_t *base)
{
    (void)base;

#if !DCL_PERSIST_SAVE
    /* ★★ 结构性闸门 (2026-09-13 降级): 放在**函数内部** ⇒ 任何调用者都绕不过去。
     *   为什么不做成"在 3 个调用点各加一句": 那只能挡住**今天已知**的三个调用点,
     *   明天新增第四个又会漏 —— 缺陷的**类别**没被消灭 (同族教训: 审计发现 F 的
     *   "三条 return 各补一句" → 改成单一出口)。 */
    g_persist_last_err = 0xD15Au;      /* "DISA" = disabled (计数器归调用方) */
    return -1;                         /* 负数 = 拒绝 (调用方按 NAK 计) */
#endif

    /* ---- ① PERSISTENT 语义门: 引擎运行中绝不落盘 (S3 同款) ----
     * 擦一个扇区 1~4 秒 = 拍长的上万倍; 期间 ISR 会持续触发而擦写又要求
     * 不从 Flash 取指/不被打断。返回 1 (不是错误) 表示"跳过, 稍后重试"。 */
    if (SHM_U8(g_shm, OFF_CTRL_ENGINE_RUN)) {
        g_persist_dirty = 1;
        return 1;
    }

    uint16_t nr = SHM_U16(g_shm, OFF_CTRL_N_ROUTES);
    uint16_t np = SHM_U16(g_shm, OFF_CTRL_N_PARAMS);
    uint16_t ns = SHM_U16(g_shm, OFF_CTRL_N_STATES);
    if (nr > MAX_ROUTES) nr = MAX_ROUTES;
    if (np > MAX_PARAMS) np = MAX_PARAMS;
    if (ns > MAX_STATES) ns = MAX_STATES;

    uint32_t plen = ((uint32_t)nr + np + ns) * 16u;

    /* ---- ② 快照 (擦除前完成, 之后不再读 SHM) ---- */
    if (nr) __builtin_memcpy(s_snap,                                  g_shm + OFF_ROUTE_TABLE, (size_t)nr * 16u);
    if (np) __builtin_memcpy(s_snap + (size_t)nr * 16u,               g_shm + OFF_PARAM_TABLE, (size_t)np * 16u);
    if (ns) __builtin_memcpy(s_snap + (size_t)(nr + np) * 16u,        g_shm + OFF_STATE_TABLE, (size_t)ns * 16u);

    /* ---- ③ 选目标副本 + 决定 seq ---- */
    PersistInfo_t info;
    persist_probe(&info);
    uint32_t tgt_sector = info.active ? PERSIST_SECTOR_B : PERSIST_SECTOR_A;
    uint32_t new_seq;
    if (info.ab_valid == PERSIST_AB_NONE)        new_seq = 1u;
    else if (info.ab_valid == PERSIST_AB_BOTH)   new_seq = ((info.seq_a > info.seq_b) ? info.seq_a : info.seq_b) + 1u;
    else if (info.ab_valid == PERSIST_AB_A)      new_seq = info.seq_a + 1u;
    else                                         new_seq = info.seq_b + 1u;

    /* ---- ④ 组 blob: [header 32B][payload][补 0xFF 到 32B 边界] ---- */
    __builtin_memset(s_blob, 0xFF, sizeof(s_blob));   /* 补齐区用 0xFF = 擦除态 */
    PersistHdr_t h;
    __builtin_memset(&h, 0, sizeof(h));
    h.magic      = PERSIST_MAGIC;
    h.version    = PERSIST_VERSION;
    h.seq        = new_seq;
    h.n_routes   = nr;
    h.n_params   = np;
    h.n_states   = ns;
    h.prog_magic = SHM_U32(g_shm, OFF_CTRL_PROG_MAGIC);
    if (plen) __builtin_memcpy(s_blob + PERSIST_HDR_SIZE, s_snap, plen);
    h.crc32 = dcl_crc32(s_blob + PERSIST_HDR_SIZE, plen);
    __builtin_memcpy(s_blob, &h, sizeof(h));

    /* blob 总长: header + payload 补齐到 32B 倍数 */
    uint32_t blen = PERSIST_HDR_SIZE + ((plen + 31u) / 32u) * 32u;

    /* ---- ⑤ 擦 → 写 → 回读 (每一步都检查) ---- */
    g_persist_target = info.active ? 1u : 0u;

    /* ★★ 审计发现 F 修复: 原来是三条 `return` 各自返回, 只有**成功路径**调了
     *   flash_lock() —— 即 erase/write/回读**任一失败**, Flash 就保持解锁状态。
     *   这与 flash.h 自己写的契约("持久化完成**必须**调用 flash_lock")直接冲突,
     *   是"防御性 API 被绕过"的典型。
     *   ★ 为什么改成单一出口而不是"在三个 return 前各补一句":
     *     补三句只是修好**今天已知**的三条路径; 明天新增第四条 return 时,
     *     同样会漏 —— 缺陷的**类别**没被消灭。
     *     单一出口把"无论成败都必须落锁"变成**结构性事实**: 新增任何失败分支
     *     都自动经过 `out:`, 漏不掉。这正是本项目"把纪律变成结构"的一贯做法
     *     (同族: build.sh 每次显式传全默认值、DCL_CAP_H723_NOTYET 的断言)。 */
    /* ★★★ 落盘窗口: 擦写期间临时放大看门狗超时。
     *   ★ 位置: 必须在**第一次碰 flash 之前**, 因为灾难点就是"擦除期间的喂狗"。
     *   ★ 只在擦写段生效 (上面的 RUN 门/参数检查都还没开窗) ⇒ 保护真空最小化。 */
    uint32_t wdt_prev = wdt_window_open();

    int r = flash_erase_sector(tgt_sector);
    if (r != FL_OK) {
        g_persist_erase_fail++;
        g_persist_last_err = (uint32_t)(-r);
        g_persist_save_fail++;
        goto out;
    }
    g_persist_erase_ok++;

    r = flash_write(flash_sector_base(tgt_sector), s_blob, blen);
    if (r != FL_OK) {
        g_persist_last_err = (uint32_t)(-r);
        g_persist_save_fail++;
        goto out;
    }

    /* ★ 回读校验: "命令返回" ≠ "写对了"。这是"落盘成功"的唯一判据。 */
    __builtin_memcpy(s_rdbuf, (const void *)(uintptr_t)flash_sector_base(tgt_sector), blen);
    if (__builtin_memcmp(s_rdbuf, s_blob, blen) != 0) {
        g_persist_last_err = 0xFFFFu;   /* 专用码: 回读不一致 */
        g_persist_save_fail++;
        r = FL_ERR_SRERROR;
        goto out;
    }

    g_persist_writes++;
    g_persist_dirty = 0;
    g_persist_save_ok++;
    g_persist_last_err = 0;
    r = FL_OK;

out:
    flash_lock();          /* ★ 单一出口: 成功/擦除失败/写失败/回读失败**都**落锁 */
    /* ★★★ 关窗也在单一出口: 无论成功、擦除失败、写失败还是回读失败, 看门狗**一定**恢复
     *   到原超时。与 flash_lock 同一个理由 —— 新增任何失败分支都自动经过这里, 漏不掉。
     *   ★ 若不在这里恢复: 一次失败就让板子**永久**停在 8s 窗口 = 保护被静默削弱
     *     (正是本项目最忌的"宣称≠实现")。 */
    wdt_window_close(wdt_prev);
    return r;
}
