/**
 * engine.h — DCL 引擎表结构与 SHM 布局 (H723 阶段 2)
 *
 * ★ 本文件是 esp32-core0 `components/core0/shared_mem.h` 的**同构子集**:
 *   容量宏、SHM 偏移、表条目结构、OP/SRC 码, 全部逐字节对齐。
 *   为什么: 迁移不变量 (MIGRATE-H723.md §1) —— 表布局与 SHM 偏移一改,
 *   S3 的 20 套回归、4 份审计记录、PC 侧按偏移读的脚本全部失效。
 *
 * 与 S3 的差异 (有意为之, 逐条说明):
 *   ① SHM 基址从"运行时 heap_caps 分配"变成**链接期 DTCM 静态区** (零等待无 cache)
 *   ② ISR 代码从 IRAM_ATTR 变成 .itcm_text 段
 *   ③ HMI 源 (SRC_HMI, 通信域写区) 本阶段未落地 — 通信域属阶段 4, 这里留位不实现
 */
#ifndef DCL_ENGINE_H
#define DCL_ENGINE_H

#include <stdint.h>
#include <stdbool.h>

/* ══════════ 容量 (与 esp32-core0 一致) ══════════ */
#define MAX_ROUTES    128
#define MAX_PARAMS    128
#define MAX_STATES    128
#define MAX_SENSORS   64
#define MAX_ACTUATORS 64
#define MAX_WIRES     128
#define MAX_LUT       256
#define MB_NREG       64    /* 通信域每区寄存器数 (阶段 2 只用于 SRC_HMI 的留位) */

/* ══════════ SHM 偏移 (逐字节对齐 esp32-core0 shared_mem.h) ══════════ */
#define OFF_SENSOR_MAP       0x0040   /* 64 × f32 */
#define OFF_ACTUATOR_STATUS  0x0140   /* 64 × f32 */
#define OFF_WIRE_MAP         0x0240   /* 128 × f32 */
#define OFF_LUT_DATA         0x0440   /* 256 × f32 */
#define OFF_ROUTE_TABLE      0x0840   /* 128 × 16B (ISR 只读) */
#define OFF_ROUTE_STAGING    0x1040   /* 128 × 16B (PC 写, ISR 热重载 memcpy) */
#define OFF_PARAM_TABLE      0x1840   /* 128 × 16B */
#define OFF_PARAM_STAGING    0x2040   /* 128 × 16B */
#define OFF_STATE_TABLE      0x2840   /* 128 × 16B */
#define OFF_STATE_STAGING    0x3040   /* 128 × 16B */
#define OFF_MB_SET           0x4AC0   /* 写区 64 WORD (SRC_HMI 源, 阶段 4 落地) */
#define SHM_SIZE             0x8000   /* 32KB (S3 为 64KB; H723 DTCM 128KB 充裕) */

/* ══════════ 路由条目 (16B packed) —— 与 S3 逐字节相同 ══════════ */
typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  src_type;
    uint8_t  src_index;
    uint8_t  dst_type;
    uint8_t  dst_channel;
    uint8_t  op;
    uint8_t  flags;
    uint16_t param_idx;
    uint16_t state_offset;
    uint16_t actuator_idx;
    uint16_t wire2_idx;
    uint8_t  period;   /* div_idx(2bit) + phase(6bit) */
} RouteEntry_t;

_Static_assert(sizeof(RouteEntry_t) == 16, "RouteEntry_t must be 16 bytes");
_Static_assert(_Alignof(RouteEntry_t) == 4, "RouteEntry_t alignment must be 4");

/* ---- 参数条目 (16B) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    float value_a;
    float value_b;
    float value_c;
    float value_d;
} ParamEntry_t;

_Static_assert(sizeof(ParamEntry_t) == 16, "ParamEntry_t must be 16 bytes");

/* ---- 状态条目 (16B) ---- */
typedef struct __attribute__((packed, aligned(4))) {
    float state_a;
    float state_b;
    float state_c;
    float state_d;
} StateEntry_t;

_Static_assert(sizeof(StateEntry_t) == 16, "StateEntry_t must be 16 bytes");

/* ---- period 字段位定义 ---- */
#define PERIOD_DIV_IDX_FAST  0   /* 1×: 每 100μs */
#define PERIOD_DIV_IDX_MID   1   /* 10×: 每 1ms */
#define PERIOD_DIV_IDX_SLOW  2   /* 100×: 每 10ms */
#define PERIOD_DIV_MASK      0x03
#define PERIOD_PHASE_SHIFT   2
#define PERIOD_PHASE_MASK    0x3F

/* ---- flags 位定义 ---- */
#define ROUTE_FLAG_ACTIVE   0x01
#define ROUTE_FLAG_WIRE2    0x02

/* ---- 原语操作码 (与 S3 完全一致) ---- */
#define OP_DIRECT   0x00
#define OP_CMP      0x01
#define OP_HYST     0x02
#define OP_CLAMP    0x03
#define OP_LPF      0x04
#define OP_PID      0x05
#define OP_RATE     0x06
#define OP_DEADBAND 0x07
#define OP_MUX      0x08
#define OP_EDGE     0x09
#define OP_LUT      0x0A
#define OP_CNT      0x0B
#define OP_TIMER    0x0C
#define OP_ARITH    0x0D
#define OP_SCALE    0x0E
#define OP_AND      0x0F
#define OP_OR       0x10
#define OP_NOT      0x11
#define OP_SR       0x12
#define OP_MAX      0x12   /* 最高有效操作码 (表填充/校验上界) */

#define OP_ARITH_ADD 0
#define OP_ARITH_SUB 1
#define OP_ARITH_MUL 2
#define OP_ARITH_DIV 3
#define OP_ARITH_MAX 4
#define OP_ARITH_MIN 5
#define OP_SR_SET_DOM    0
#define OP_SR_RESET_DOM  1

/* ---- 源类型 ---- */
#define SRC_SENSOR  0
#define SRC_WIRE    1
#define SRC_CONST   2
#define SRC_HMI     3   /* 通信域设定区 — 阶段 4 落地 (本阶段 read_source 返回 0) */

/* ---- 目标类型 ---- */
#define DST_WIRE    2

/* ---- dt 感知 (秒) —— 与 S3 primitives.h 同口径 ---- */
#define DT_FAST  0.0001f   /* div0: 100μs */
#define DT_MID   0.001f    /* div1: 1ms  */
#define DT_SLOW  0.01f     /* div2: 10ms */

/* ══════════ 编译期布局断言 (S3 A4 纪律: 任何区域不得重叠) ══════════ */
_Static_assert(OFF_SENSOR_MAP      + MAX_SENSORS   * 4 <= OFF_ACTUATOR_STATUS, "SHM: SENSOR_MAP 越界");
_Static_assert(OFF_ACTUATOR_STATUS + MAX_ACTUATORS * 4 <= OFF_WIRE_MAP,        "SHM: ACTUATOR_STATUS 越界");
_Static_assert(OFF_WIRE_MAP        + MAX_WIRES     * 4 <= OFF_LUT_DATA,        "SHM: WIRE_MAP 越界");
_Static_assert(OFF_LUT_DATA        + MAX_LUT       * 4 <= OFF_ROUTE_TABLE,     "SHM: LUT_DATA 越界");
_Static_assert(OFF_ROUTE_TABLE     + MAX_ROUTES    * 16 <= OFF_ROUTE_STAGING,  "SHM: ROUTE_TABLE 越界");
_Static_assert(OFF_ROUTE_STAGING   + MAX_ROUTES    * 16 <= OFF_PARAM_TABLE,    "SHM: ROUTE_STAGING 越界");
_Static_assert(OFF_PARAM_TABLE     + MAX_PARAMS    * 16 <= OFF_PARAM_STAGING,  "SHM: PARAM_TABLE 越界");
_Static_assert(OFF_PARAM_STAGING   + MAX_PARAMS    * 16 <= OFF_STATE_TABLE,    "SHM: STATE_TABLE 越界");
_Static_assert(OFF_STATE_TABLE     + MAX_STATES    * 16 <= OFF_STATE_STAGING,  "SHM: STATE_TABLE 越界");
_Static_assert(OFF_STATE_STAGING   + MAX_STATES    * 16 <= OFF_MB_SET,         "SHM: STATE_STAGING 越界");
_Static_assert(OFF_MB_SET          + MB_NREG       * 2  <= SHM_SIZE,           "SHM: MB_SET 越界");

/* ══════════ 引擎扫描 (两份实例: FLASH 与 ITCM, 见 engine.c) ══════════
 * @param base  SHM 基址 (DTCM 内)
 * @param n     本拍扫描的路由条数 (0..MAX_ROUTES)
 * @return      校验和 (证明"真的算过" —— 防死代码消除 + 提供运行证据)
 */
typedef uint32_t (*engine_scan_fn)(uint8_t *base, uint32_t n);

extern uint32_t engine_scan_flash(uint8_t *base, uint32_t n);
extern uint32_t engine_scan_itcm (uint8_t *base, uint32_t n);

/** @brief 按 profile 填充参数表/状态表/路由表 (冷启动与重配置共用)
 *  profile: 0 = 全 DIRECT (对照 S3 的 234 cyc 基线)
 *           1 = 19 原语轮转 (混合程序, 真实成本谱)
 *           2 = 全 PID (最重档, 探预算上界) */
void engine_fill_tables(uint8_t *base, int profile);

/** @brief SHM 静态区 (定义在 engine.c, 链接段 .dtcm_shm / DTCM 0x20000000) */
extern uint8_t g_shm[SHM_SIZE];

/** @brief 冷启动清零 (.dtcm_shm 是 NOLOAD, 上电内容不确定 → 必须显式清) */
void cold_start_reset(void);

/** @brief 落位自检: 1 = 声明位置与 .dtcm_shm 段首吻合且落在 DTCM 域内 */
int shm_layout_ok(void);

#endif /* DCL_ENGINE_H */
