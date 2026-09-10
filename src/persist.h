/**
 * persist.h — 配置掉电保持 (裸 Flash 双副本 A/B)
 *
 * ══════════════════════════════════════════════════════════════════════
 * 与 S3 (esp32-core0/components/persist/persist.h) 的关系
 * ══════════════════════════════════════════════════════════════════════
 * **语义逐字搬, 介质与原子性手法必须重写。**
 *
 * S3 用 NVS 分区 + 单副本 + CRC 兜底, 并在自己的注释里明确写了这条欠账:
 *   > ★若需"断电也不丢配置", 需 A/B 双副本 + 序号 (写 B 成功再切指针)
 *   > ★安全边界 = 宁可丢配置, 绝不加载坏表 (CRC 兜底, 不是硬件原子性保证)
 * H723 直接还这笔账: **双副本 A/B + 单调序号**, 使"擦除中掉电"不再丢配置。
 *
 * 保留下来的 S3 语义 (一条都不能改):
 *   ① **只持久化"部署的组态", 不持久化运行状态** —— 上电恢复表但引擎保持 STOP,
 *      由 PC 显式 START。执行器不会在无人监督下上电即动 (安全语义)。
 *   ② **版本不符 = 忽略旧表**(不是"尽力解释") —— 参数语义迁移后按新语义解释旧表
 *      会静默错算; 宁可上电空配置。
 *   ③ **CRC 不过 = 忽略** —— 宁可丢配置, 绝不加载坏表。
 *   ④ **运行期 0 Flash 操作** —— 擦写只在 STOP 窗口; 运行中 deploy 只热重载。
 *      这不是性能优化, 是确定性承诺的一部分 (擦一个扇区 1~4 秒 = 拍长的上万倍)。
 *
 * ══════════════════════════════════════════════════════════════════════
 * Flash 布局 (H723ZGT6: 1MB 单 bank, 8 × 128KB 扇区)
 * ══════════════════════════════════════════════════════════════════════
 *   sector 6 @ 0x080C0000  128KB   = 副本 A
 *   sector 7 @ 0x080E0000  128KB   = 副本 B
 *
 * ★ 为什么是 6/7 而不是规划里写的 14/15: 规划那组编号是照 **2MB 双 bank H743**
 *   抄的 (bank2 从 sector 8 起编号)。**H723ZGT6 实测只有 bank1** (读 0x52002100
 *   即 bank2 的 KEYR2 得 0), 扇区号只能 0..7。选末尾两个: 固件 bin 目前 18KB
 *   (占 sector 0), 扇区 6/7 远离代码区, 将来固件长到几百 KB 也不会撞上。
 *
 * 每份布局:
 *   offset 0x000: header (32B, 下面 PersistHdr_t)
 *   offset 0x020: payload (route表 | param表 | state表), 各 n×16B
 *   payload 之后到 32B 边界补 0xFF (H7 必须先擦后写, 写半行会破坏整行)
 *
 * ★★ 双副本的写入策略 ("写旧的" 而不是 "轮流写"):
 *   读两份 header, 比较 seq, **往 seq 较小的那份写**(先擦后写)。
 *   ⇒ 任何时刻至少有一份是完整的 (刚写的那份 + 没动的那份)。
 *     即使擦除到一半掉电, 另一份的 seq 仍较大且 CRC 完好, 重启后能加载。
 *   ⇒ 这就是"真掉电判据"能成立的原因: 不是"CRC 兜底"的运气, 是结构性保证。
 *   若两份都无效 (首次上电/都损坏) → 选 A 写 (并使 seq=1)。
 *
 * 序号单调递增: 每次成功 save 后 seq++。不需要 64 位 —— 32 位在任何现实
 * 擦写次数下都不会回绕 (128KB 扇区寿命 10 万次量级)。
 */
#ifndef DCL_PERSIST_H
#define DCL_PERSIST_H

#include <stdint.h>
#include <stddef.h>

/* ---- 副本所在扇区 ---- */
#define PERSIST_SECTOR_A   6u
#define PERSIST_SECTOR_B   7u

/* ---- header ---- */
#define PERSIST_MAGIC      0x504C4350u   /* "PLCP" —— H723 线另起, 与 S3 的 "DCLP" 区分
                                          * ★ 不复用 S3 的 0x504C4344: 介质不同 (NVS vs 裸扇区),
                                          *   布局不同, 用同一 magic 会让"误把 S3 镜像当 H723 表"
                                          *   变得可能。 */
#define PERSIST_VERSION    0x0200u       /* H723 线 v2.0 (与 DCL_FW_VERSION_H723 同谱系) */
#define PERSIST_HDR_SIZE   32u           /* 对齐到 32B (一个 flash word) */

typedef struct __attribute__((packed, aligned(4))) {
    uint32_t magic;      /* PERSIST_MAGIC */
    uint32_t version;    /* PERSIST_VERSION */
    uint32_t seq;        /* 单调递增序号 (越大越新) */
    uint32_t crc32;      /* CRC32 覆盖 payload (不含本 header) */
    uint16_t n_routes;   /* 路由条数 (已归组序) */
    uint16_t n_params;
    uint16_t n_states;
    uint16_t reserved;   /* 显式命名尾部填充: 使逐字节比对不依赖填充内容
                          * (S3 RouteEntry_t 的同款教训: 填充变脏 → 校验和漂移) */
    uint32_t prog_magic; /* 部署时的 PROG_MAGIC 快照 (证明"这份表确实部署过") */
    uint32_t reserved2;  /* 补到 32B (= 一个 flash word) */
} PersistHdr_t;

_Static_assert(sizeof(PersistHdr_t) == PERSIST_HDR_SIZE,
               "PersistHdr_t must be exactly 32 bytes (one flash word)");

/* ══════════════════ 状态查询 (供 0x43 命令) ══════════════════ */
#define PERSIST_AB_NONE    0u   /* 两份都无效 (首次上电 / 都损坏) */
#define PERSIST_AB_A       1u   /* 只有 A 有效 */
#define PERSIST_AB_B       2u   /* 只有 B 有效 */
#define PERSIST_AB_BOTH    3u   /* 两份都有效 (正常态) */

typedef struct {
    uint8_t  ab_valid;    /* 上述 PERSIST_AB_* */
    uint8_t  active;      /* 当前加载/将要写的是 A(0) 还是 B(1) */
    uint16_t n_routes;
    uint16_t n_params;
    uint16_t n_states;
    uint32_t seq_a;
    uint32_t seq_b;
    uint32_t crc_a;       /* 0 = A 无效 */
    uint32_t crc_b;
} PersistInfo_t;

/* ---- 观测面 (pyocd 读; 每个都必须"被读过一次"否则被 --gc-sections 回收) ---- */
extern volatile uint32_t g_persist_save_ok;
extern volatile uint32_t g_persist_save_fail;
extern volatile uint32_t g_persist_load_ok;
extern volatile uint32_t g_persist_load_fail;
extern volatile uint32_t g_persist_load_seq;
extern volatile uint32_t g_persist_last_err;   /* flash 层错误码 */
extern volatile uint32_t g_persist_writes;     /* 累计落盘次数 */
extern volatile uint32_t g_persist_dirty;      /* 1 = 有未落盘配置 */
extern volatile uint32_t g_persist_target;     /* 最近一次写的是 A(0)/B(1) */
extern volatile uint32_t g_persist_erase_ok;
extern volatile uint32_t g_persist_erase_fail;

/** @brief 探测两份副本, 填 PersistInfo_t (不修改任何东西; 纯读)
 *  @return FL 风格错误码; 0 = 探测完成 (ab_valid 说明结果) */
int persist_probe(PersistInfo_t *out);

/** @brief 上电加载: 读 A/B 中 seq 较大且 CRC 通过的那份, 直接写进 ACTIVE 表。
 *
 *  ★ 必须在**引擎启动前**、且 ENGINE_RUN=0 时调用 (此刻无并发扫描, 无撕裂风险)。
 *    S3 的同名教训: 不要走 STAGING+RELOAD 路径 —— 启动流程会清 RELOAD,
 *    导致 staging 永不 memcpy 进 ACTIVE (症状: N_ROUTES=1 但表全 0)。
 *
 *  ★ 恢复后**引擎保持 STOP** (不置 ENGINE_RUN): 安全语义, 与 S3 一致。
 *
 *  @return >0 = 恢复的条目总数; 0 = 无有效副本 (空配置, 正常); <0 = 错误 */
int persist_load(uint8_t *base);

/** @brief 落盘当前 ACTIVE 表 (擦"旧"的那份副本 → 写 → 回读校验)
 *
 *  ★★ 调用前提 (缺一不可):
 *    ① ENGINE_RUN == 0 (STOP 窗口) —— 擦写期间不得有拍在跑,
 *       否则 1~4 秒的擦除会把拍周期彻底打乱 (且 ISR 从 Flash 取指会违规)
 *    ② 不在 ISR 里 (本函数是同步阻塞的, 会自旋等 QW)
 *  违反①时本函数**直接返回不落盘**(保持 dirty), 由调用方在 STOP 后重试 ——
 *  这是"S3 的 PERSISTENT 语义门"在裸机上的等价物。
 *
 *  @return 0 = 成功落盘; <0 = 失败 (g_persist_last_err 带细因); 1 = 因引擎运行中而跳过 */
int persist_save(uint8_t *base);

/** @brief 失败原因码 → 短文本 (给 NAK 用) */
const char *persist_err_str(int r);

#endif /* DCL_PERSIST_H */
