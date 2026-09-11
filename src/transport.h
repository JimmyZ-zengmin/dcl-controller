/**
 * transport.h — DCL 上位机↔控制器二进制帧协议
 *
 * ══════════════════════════════════════════════════════════════════
 * ★ 协议部分**逐字沿用** esp32-core0/components/transport/uart_protocol.h
 * ══════════════════════════════════════════════════════════════════
 * 为什么必须逐字: MIGRATE-H723.md 阶段 3 的验收标准写的是
 *   「20 套回归全绿, **脚本零改动**」
 * —— 上位机脚本是既有资产, 协议一变它们全部失效。所以帧格式、命令码、
 *    CRC 多项式、长度上限一个字节都不能动。
 *
 * 唯一新增的是文件末尾的 **H723 平台段**(版本号与能力位图) —— 那不属"协议",
 * 属"设备自述"。
 *
 * 帧格式: [SYNC:1B][CMD:1B][LEN:2B LE][PAYLOAD:LEN][CRC16:2B LE]
 *   · SYNC: PC→MCU = 0xC0, MCU→PC = 0xC1
 *   · CRC16-CCITT (poly 0x1021, init 0xFFFF), 覆盖 [CMD][LEN_LO][LEN_HI][PAYLOAD]
 *     —— **不含 SYNC**, 因为两个方向的 CRC 算法要一致
 */
#ifndef DCL_TRANSPORT_H
#define DCL_TRANSPORT_H

#include <stdint.h>
#include <stddef.h>

#define FRAME_SYNC_PC2MCU   0xC0
#define FRAME_SYNC_MCU2PC   0xC1

/* 0x00-0x0F 保留给系统/协商类命令 */
#define CMD_GET_VERSION     0x01   /* 版本/能力协商 (审计 F4): 无载荷 → ACK 4B
                                      [fw_ver:u16][cap:u16], 见 DCL_CAP_* */
#define CMD_DEPLOY          0x10
#define CMD_START           0x11
#define CMD_STOP            0x12
#define CMD_RESET           0x13
#define CMD_READ            0x20
#define CMD_WRITE           0x21
#define CMD_READ_BURST      0x22
#define CMD_WRITE_BURST     0x23
#define CMD_FORCE           0x24   /* P2: 强制/释放 wire — [idx:u16][mode:u8][val:f32] */
#define CMD_ENGINE_STATUS   0x38
#define CMD_PERSIST         0x43   /* W2.4: 掉电保持查询/落盘 — 空=查询, [mode:u8]=1 落盘
                                    *
                                    * ★★ PC 侧必读: [mode=1] 的 **ACK 延迟 = 擦除耗时**。
                                    *   H7 最小擦除粒度是整扇区 128KB, 实测 0.5~1s
                                    *   (串口实测 0.84s; 极端情况可到数秒)。
                                    *   ⇒ **PC 侧 timeout 必须 ≥ 4 秒**。
                                    *   若用默认 0.6s, 会把"正在擦除"误判成 NAK 或超时 ——
                                    *   而**落盘其实成功了** (查 0x43 空载荷可见条数已更新)。
                                    *   这与 h723_persist.py 的 pyocd 路径不同: 那条走
                                    *   g_persist_req 直写, 不受串口 timeout 影响。
                                    *   (本次实测踩到: 0.6s 超时被脚本判成 NAK, 而 flash 里
                                    *    确实已经写好了 8 条 —— "命令没回" ≠ "命令没做"。) */
#define CMD_SEQ_DEPLOY      0x44   /* Sequencer v0: 部署顺序域 (设计 D6: 独立命令
                                      不往 0x10 塞 — 3078B 压线教训) */
#define CMD_MACRO           0x40
#define CMD_DISPLAY_INIT    0x51   /* 固件原生 — ST7735 初始化 */
#define CMD_DISPLAY_FILL    0x52   /* 固件原生 — 填充矩形 */
#define CMD_DISPLAY_TEXT    0x53   /* 固件原生 — 绘制文字 */
/* 通信域 COMM (Modbus RTU 从站) — 隧道模式: 零硬件验证协议栈。
 * 字节源切换为 UART1 FIFO 后这两个命令仍保留 (调试/回归用) */
#define CMD_MB_INJECT       0x60   /* 注入一帧 Modbus RTU 请求 → ISR 状态机消费 */
#define CMD_MB_RESP         0x61   /* 读回响应帧 + 通信域状态 */
#define CMD_MB_CFG          0x62   /* 配置通信域: [src u8][tx_uart u8][budget u8]
                                    * (tx_uart=1: 响应从 UART1 物理口发 → LA 可抓) */

#define STS_ACK             0x00
#define STS_NAK             0xFF

/* ---- 固件版本 (FW_VERSION) 与能力位图 (审计 F4) ----
 * GET_VERSION 返回 [fw_ver:u16][cap:u16]; 上位机据此判断能力分支,
 * 不再靠"NAK 文本"猜 (旧 PC 连新固件 / 新 PC 连旧固件都能优雅降级) */
#define DCL_FW_VERSION      0x0107   /* (S3 线) v1.7: + SRC_HMI 设定值源 + AI 模拟量输入 */
#define DCL_CAP_MULTICYCLE  0x0001   /* 多周期 div 档 (100μs/1ms/10ms) */
#define DCL_CAP_HOTRELOAD   0x0002   /* deploy 热重载 (staging→ACTIVE ≤1 拍) */
#define DCL_CAP_PERSISTENT  0x0004   /* 掉电保持 (运行期 0 flash 操作) */
#define DCL_CAP_STATE_COLD  0x0008   /* RESET/deploy 状态冷启动 (审计 M2) */
#define DCL_CAP_WIRE2_FLAG  0x0010   /* wire2_valid 显式标志 (审计 F2) */
#define DCL_CAP_VERINFO     0x0020   /* GET_VERSION 命令可用 */
#define DCL_CAP_SEQ         0x0040   /* 顺序域 Sequencer (0x44 SEQ_DEPLOY, 审计 OA4) */
#define DCL_CAP_FORCE       0x0080   /* wire 强制/释放 (0x24, P2) */
#define DCL_CAP_COMM        0x0100   /* 通信域 Modbus RTU 从站 (0x60/0x61/0x62) */
#define DCL_CAP_HMI         0x0200   /* SRC_HMI 设定值源 (DSL 引用 40065+, OA21/v1.7) */
#define DCL_CAP_AI          0x0400   /* AI 模拟量输入组件 (SENSOR[8..10], v1.7) */

/* N2 (外部审计): 原 1024 使 WRITE_BURST count=255/256 的请求帧 (6+count×4 > 1024)
 * 在解析层被静默丢弃 (TIMEOUT 无 NAK), 与 READ_BURST 响应 1030B 不对称。
 * 提到 1030: 请求 payload 上限 = WRITE_BURST 256 字 (6+1024), 读写对称 256 字 */
/* F1 (审计九): 扩容 64→128 后三表满 = 384 条目 (128 route + 128 param + 128 state)
 * payload = 6 + 384×16 = 6150B, 单帧满表部署. FrameParser_t.payload 静态 RAM 增
 * ~3KB (g_parser 全局). fp_feed CRC 已改分块计算 — 不再有栈上大缓冲.
 * N7 历史注: 原 3078 = 6 + 192×16 (64+64+64) 的设计容量单帧下发修复. */
#define FRAME_PAYLOAD_MAX   6150
#define FRAME_TOTAL_MAX     (FRAME_PAYLOAD_MAX + 6)

uint16_t crc16_ccitt(const uint8_t *data, size_t len);
uint16_t crc16_ccitt_seg(uint16_t crc, const uint8_t *data, size_t len);

typedef struct {
    uint8_t  state;     /* 0=WAIT_SYNC 1=CMD 2=LEN_LO 3=LEN_HI 4=PAYLOAD 5=CRC_LO 6=CRC_HI */
    uint8_t  cmd;
    uint16_t payload_len;
    uint16_t payload_idx;
    uint8_t  payload[FRAME_PAYLOAD_MAX];
    uint8_t  crc_lo;
} FrameParser_t;

void fp_init(FrameParser_t *fp);
int  fp_feed(FrameParser_t *fp, uint8_t byte); /* 0=waiting 1=ok -1=bad */

/* ═══════════════ H723 平台段 (新增; 上面的协议定义逐字未改) ═══════════════
 *
 * ★★ 项目第一铁律「宣称 = 实现」在这里的落点:
 *    能力位图只声明**真的跑通了**的能力。
 *    S3 报 cap = 0x07FF (11 项全有); H723 阶段 3.1 只有「分档调度」和
 *    「版本协商」两项落地, 所以**只报 2 位**。
 *    旧上位机据此优雅降级 —— 不会去调不存在的命令、然后拿到 TIMEOUT 而误判
 *    "固件挂了"(这正是 F4 协商惯例存在的理由)。
 *
 * 版本号取 0x0200 而不是接着 S3 的 0x0107: 平台换了, 版本谱系另起。
 * 上位机凭 fw_ver 高位即可判断"这是 H723 线", 从而选择不同的行为。 */
#define DCL_FW_VERSION_H723   0x0200u

/* ---- 逐条列出**未声明**的能力与原因 (防止日后"顺手"把它报上去) ----
 *   DCL_CAP_STATE_COLD(0x0008) — RESET(0x13) 命令**已存在**, 但"状态冷启动"语义
 *                                (deploy 时清 state 表) 由 engine_reload_active 的
 *                                M2 清零实现 —— 待 W2 收口时与 S3 口径核对后决定是否声明
 *   DCL_CAP_HMI       (0x0200) — SRC_HMI 是**留位**(engine.c 显式 case, 恒返 0)
 *   DCL_CAP_AI        (0x0400) — ADC 未接 (W5)
 * ★ 这份清单同时是**上线检查表的雏形**: 每落地一项就在这里删一行、在上面的
 *   宏里加一位 —— 两处必须同步, 否则就是"报了个没实现的"或"实现了却不报"。
 *   ★ A4 事故 (2026-09-10): 阶段 3.2 落地了热重载, 却忘了改这里 —— 正是
 *     "实现了却不报"。评审提醒: 这类漏改**没有任何编译期保护**, 只能靠纪律 +
 *     上面的清单与下面的宏**在同一屏内可见**(所以刻意放在一起)。
 *   ★ W3 (2026-09-11): DCL_CAP_SEQ 已从上面的未声明清单**删除并加入下面的宏**
 *     —— tools/h723_seq.py 27 项全绿 (T28 灵魂测试 14 + T29 校验器 13) 为凭据。
 *   ★ W4 (2026-09-11): DCL_CAP_COMM 同样已移入下面的宏 (0x0040→0x00F7→0x01F7)
 *     —— Modbus RTU 从站 (0x60/0x61/0x62) 落地; 证据见 tools/h723_modbus.py。
 *   ★★ 纪律提醒 (已在 W3 吃过一次): **改能力位必须同步 tools/h723_proto.py 的
 *     EXPECT_CAP** —— 那里是第二个手工副本, 曾经停在 0x0033 三轮未同步。 */
#define DCL_CAP_H723_IMPL   (DCL_CAP_MULTICYCLE | DCL_CAP_HOTRELOAD | \
                             DCL_CAP_PERSISTENT | DCL_CAP_WIRE2_FLAG | \
                             DCL_CAP_VERINFO | DCL_CAP_FORCE | \
                             DCL_CAP_SEQ | DCL_CAP_COMM)              /* = 0x01F7 */

/* ★ 上线的各项说明 (写清楚"为什么现在可以报"):
 *   DCL_CAP_HOTRELOAD (0x0002) — 阶段 3.2: engine_reload_active() 在 ITCM 内
 *     切换 ACTIVE 表, 且 APPLIED_SEQ 回读确认 (0x10 ACK 带 seq/budget,
 *     0x38 尾部带 deploy_seq/applied_seq/applied_lat)。自检 9/9。
 *   DCL_CAP_PERSISTENT(0x0004) — W2.4: 裸 Flash 双副本 A/B (扇区 6/7),
 *     CMD_PERSIST(0x43) 可查询/落盘。★ 声明它的含义包含"运行期 0 flash 操作":
 *     persist_save() 在 ENGINE_RUN=1 时**直接跳过**(g_persist_skip_run 可证),
 *     擦写只发生在 STOP 窗口。掉电判据: 擦除中复位仍能加载旧副本 (双副本结构性保证)。
 *   DCL_CAP_WIRE2_FLAG(0x0010) — A3 修复后, ISR 的第二输入判据改为
 *     `wire2_valid(flags, wire2_idx)` = (显式标志 || 非 0 索引) && 索引合法,
 *     即**真的按 ROUTE_FLAG_WIRE2 标志办事**了 (此前标志被定义但从未被引用)。
 *   DCL_CAP_FORCE     (0x0080) — W2: CMD_FORCE(0x24) 落地, 拍首覆写 + 写端屏蔽
 *     两半都在 (engine_tick / DEFINE_ENGINE_SCAN), 且 FORCE_VAL 列入 float 区。
 *     ★ 验收证据必须是**非零强制值** (OA9 事故的判据盲区修正)。 */
#define DCL_CAP_H723_NOTYET (DCL_CAP_STATE_COLD | \
                             DCL_CAP_HMI | DCL_CAP_AI)
_Static_assert((DCL_CAP_H723_IMPL & DCL_CAP_H723_NOTYET) == 0u,
               "cap bitmap contradiction: bit present in BOTH impl and not-yet lists");

#endif /* DCL_TRANSPORT_H */
