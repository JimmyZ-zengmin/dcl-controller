/* prog_store.h — DCL 程序持久化 (SD 卡 A/B 双副本) — 2026-09-15 (S4)
 *
 * 契约: docs/REF-program-contract.md §6 (SD 分区) + §7 ("不能漏错"五道闸)
 *
 * 为什么需要它
 * ------------
 * 用户口径: **用这个系统 = 写 DCL 程序**; 程序必须能走通信上传、必须持久化,
 * 但**不能走传统 flash 烧录** (烧录的是引擎核心)。而内部 flash 那条路已被降级
 * (`DCL_PERSIST_SAVE=0`, 擦 128KB 时 ISR 卡死 210ms ⇒ 看门狗复位)。
 * ⇒ 程序落到 **SD 卡的卡尾两区** (由 sd.c 的 S3 分区划出, 日志碰不到)。
 *
 * 五道闸在这里的实现
 * ------------------
 *   闸3 "写完立即回读比对"   → payload 写完逐块读回 memcmp, 不符则**不写头** (该副本作废)
 *   闸4 "A/B 双副本 + 单调 seq" → seq 取 max+1; 任意时刻至少一份完整
 *   闸5 "装载前重跑上传时同一套静态校验" → 由上层传入 prog_validate 回调, **不在这里重写一份**
 *   (闸1 帧 CRC16 在协议层; 闸2 清单 CRC32 在 COMMIT 处)
 *
 * ★ 头**最后写**是原子性的关键: 载荷写一半掉电 ⇒ 旧头还在 ⇒ seq 不变 ⇒ 那份仍是"旧的有效副本"。
 * ★ 顺序: 先写**无效/较旧**的那一份。这样即使写坏, 另一份始终可用。
 */
#ifndef DCL_PROG_STORE_H
#define DCL_PROG_STORE_H

#include <stdint.h>
#include "sd.h"      /* ★ SD_PROG_BLOCKS / SD_BLK_SZ —— 单一来源, 不在本文件重定义 */

#define PROG_HDR_MAGIC    0x504C4350u                  /* "PCLP" */
#define PROG_HDR_VERSION  1u
#define PROG_PAYLOAD_MAX  ((SD_PROG_BLOCKS - 1u) * 512u)   /* 15 块 × 512 = 7680 B */

/* 返回码 (对外可读, 不要用哨兵值) */
#define PROG_RC_OK        0u
#define PROG_RC_NOPART    1u   /* 卡容量不足, 未划出程序区 */
#define PROG_RC_LEN       2u   /* 长度非法 (0 或超过 PROG_PAYLOAD_MAX) */
#define PROG_RC_WRITE     3u   /* 卡写失败 */
#define PROG_RC_VERIFY    4u   /* ★ 回读比对不符 (闸3) */
#define PROG_RC_NONE      5u   /* 两份都没有有效副本 */
#define PROG_RC_CRC       6u   /* CRC32 不符 */
#define PROG_RC_VALIDATE  7u   /* ★ 静态校验拒绝 (闸5) */
#define PROG_RC_MINF      8u   /* 程序要求的固件版本高于本机 */
#define PROG_RC_CAPS      9u   /* 程序要求的能力位本机没有 */

/* 清单 (契约 §4.3)。★ 它放在**副本头**里, 不进帧 —— 因为 0x10 载荷满配时
 *   刚好等于 FRAME_PAYLOAD_MAX(6150), 一个字节余量都没有 (契约 GAP-2)。 */
typedef struct {
    uint32_t prog_id;
    uint16_t prog_ver;
    uint16_t min_fw;      /* 低于此固件版本 ⇒ 拒绝 */
    uint32_t req_caps;    /* 程序要求的能力位 (u32) */
} ProgManifest_t;

/* 副本头 (占副本第 0 块, 512B 内) */
typedef struct {
    uint32_t magic;
    uint32_t version;
    uint32_t seq;         /* 单调递增: 读两份取 seq 大者 */
    uint32_t crc32;       /* 覆盖 payload[0..len) */
    uint32_t len;         /* payload 字节数 */
    uint32_t prog_id;
    uint16_t prog_ver;
    uint16_t min_fw;
    uint32_t req_caps;
    uint32_t fw_ver;      /* 写入时的固件版本 */
    uint16_t n_routes;
    uint16_t n_params;
    uint16_t n_states;
    uint16_t reserved;
    uint32_t hdr_sum;     /* 头自身的校验和 (覆盖 hdr_sum 之前的全部字) */
} ProgCopyHdr_t;

/* 状态快照 (供 0x48 与外部审计读走) */
typedef struct {
    uint32_t part_ok;      /* 1 = 卡已分区, 程序存储可用 */
    uint32_t ab_valid;     /* bit0 = A 头有效, bit1 = B 头有效 */
    uint32_t seq_a, seq_b;
    uint32_t crc_a, crc_b; /* 实测 CRC (0 = 该副本无效) */
    uint32_t len_a, len_b;
    uint32_t active;       /* 0 = A, 1 = B, 0xFFFFFFFF = 无 */
    uint32_t ok_n, fail_n, reject_n;
    uint32_t last_rc;
    uint32_t loaded_n;     /* 开机装载成功的载荷字节数 (0 = 没装) */
    uint32_t boot_reject;  /* 1 = 开机装载被静态校验拒绝 (闸5 生效) */
} ProgStoreInfo_t;

/* 上层提供的静态校验回调: 返回 NULL = 通过, 否则拒绝串。
 * ★ 由 main.c 传入 `prog_validate` —— 保证"装载时与上传时是同一套校验"。 */
typedef const char *(*prog_validate_fn)(const uint8_t *p, uint32_t n, uint32_t *budget_out);

/* 覆盖写: 写到"无效 / 较旧"的那一份, seq = max+1。成功返回 PROG_RC_OK */
int  prog_store_save(const uint8_t *payload, uint32_t len, const ProgManifest_t *mf);
/* 读出"有效且 seq 最大"的那一份。成功返回 PROG_RC_OK 并把清单写进 *mf_out */
int  prog_store_load(uint8_t *out, uint32_t cap, ProgManifest_t *mf_out);
/* 两份头都清零 (程序作废)。成功返回 PROG_RC_OK */
int  prog_store_erase(void);
/* 体检: 两份头的有效性 / seq / 实测 CRC / 长度 */
void prog_store_probe(ProgStoreInfo_t *o);
/* 开机装载: 读 → 校验(闸5, 用 vfn) → 交给上层部署。返回 PROG_RC_OK 表示"可以部署" */
int  prog_store_boot_load(uint8_t *scratch, uint32_t cap, prog_validate_fn vfn);

/* 上传事务与落盘**共用同一个载荷缓冲** —— 避免在 DTCM 里摆两份 7.5KB。
 * ★ 上传路径把它当收件箱, 落盘路径把它当源; `prog_store_save` 内部已处理"源==缓冲"的情形。 */
uint8_t *prog_store_buf(void);
uint32_t prog_store_buf_sz(void);

extern volatile uint32_t g_prog_ok_n, g_prog_fail_n, g_prog_reject_n;
extern volatile uint32_t g_prog_last_rc, g_prog_loaded_n, g_prog_active_copy;
/* ★ GAP-11: **卡上那份副本的载荷长度**（可能比"由 counts 推出来的长度"多 48B —— 那是绑定表段）。
 *   为什么单独一个量: `g_prog_loaded_n` 是"由 counts 推出"的长度（= 校验用的形状）,
 *   而定位载荷**尾部**那一段必须用**实际存储长度**。两者混用就会读错位置（同族: 一个值两个语义）。*/
extern volatile uint32_t g_prog_payload_len;
extern volatile uint32_t g_prog_boot_reject;
extern const char *volatile g_prog_reject_str;   /* 最近一次拒绝串 (调试读, 不保证跨语义稳定) */

#endif /* DCL_PROG_STORE_H */
