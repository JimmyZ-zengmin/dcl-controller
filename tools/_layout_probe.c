/* 布局探针: 用 _Static_assert 在编译期证明 Python 侧假设的结构体偏移。
 * 编译通过 = 假设成立; 失败 = 报出哪个字段错了。 */
#include <stdint.h>
#include <stddef.h>

typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  cond_type; uint8_t  cond_idx; uint8_t  flags; uint8_t  reserved;
    uint16_t param_idx; uint16_t state_offset; uint16_t jump_idx;
    uint32_t reserved2;
} S;

typedef struct __attribute__((packed, aligned(4))) {
    uint16_t step_base; uint16_t n_steps; uint16_t step_cur; uint16_t out_wire;
    uint8_t  period; uint8_t run; uint16_t reserved; uint32_t step_tick;
} C;

typedef struct __attribute__((packed, aligned(4))) {
    uint8_t  src_type, src_index, dst_type, dst_channel;
    uint8_t  op, flags; uint16_t param_idx, state_offset, actuator_idx, wire2_idx;
    uint8_t  period, reserved;
} R;

/* ★★ 重要修正 (本次探针的目的就是抓这类错):
 *   我原本以为 `packed` 结构体里 u32 会落在 offset 12 (4×u8 + 3×u16 = 10 之后
 *   再补到 12 对齐)。**错**。`packed` 的语义就是**不做任何对齐填充**:
 *       4×u8 @0..3, 3×u16 @4/6/8, 然后 u32 直接接在 **offset 10**。
 *       总长 = 10 + 4 = 14, `aligned(4)` 再把 sizeof 补到 16 (尾部 2 字节填充)。
 *   ⇒ 内部偏移是 10, 不是 12; 只有**结构体总长**是 16。
 *   Python 侧的正确拼法 = `<BBBBHHHIxx` (14 字节内容 + 2 字节尾填充 = 16),
 *   与下面的断言一致。★ 若写成 `@12` / `@8` 就静默错位了。 */
_Static_assert(sizeof(S) == 16, "S sizeof");
_Static_assert(offsetof(S, cond_type)    == 0,  "S.cond_type");
_Static_assert(offsetof(S, cond_idx)     == 1,  "S.cond_idx");
_Static_assert(offsetof(S, flags)        == 2,  "S.flags");
_Static_assert(offsetof(S, reserved)     == 3,  "S.reserved");
_Static_assert(offsetof(S, param_idx)    == 4,  "S.param_idx");
_Static_assert(offsetof(S, state_offset) == 6,  "S.state_offset");
_Static_assert(offsetof(S, jump_idx)     == 8,  "S.jump_idx");
_Static_assert(offsetof(S, reserved2)    == 10, "S.reserved2 (packed → 紧跟 jump_idx)");

/* SeqCtrl_t: packed → 4×u16 @0/2/4/6, 2×u8 @8/9, u16 @10, u32 @12 (10+2=12 恰好)
 *   总长 = 16, aligned(4) 不需补。 */
_Static_assert(sizeof(C) == 16, "C sizeof");
_Static_assert(offsetof(C, step_base) == 0,  "C.step_base");
_Static_assert(offsetof(C, n_steps)   == 2,  "C.n_steps");
_Static_assert(offsetof(C, step_cur)  == 4,  "C.step_cur");
_Static_assert(offsetof(C, out_wire)  == 6,  "C.out_wire");
_Static_assert(offsetof(C, period)    == 8,  "C.period");
_Static_assert(offsetof(C, run)       == 9,  "C.run");
_Static_assert(offsetof(C, reserved)  == 10, "C.reserved");
_Static_assert(offsetof(C, step_tick) == 12, "C.step_tick");

/* RouteEntry_t: 16B */
_Static_assert(sizeof(R) == 16, "R sizeof");
_Static_assert(offsetof(R, src_type)    == 0,  "R.src_type");
_Static_assert(offsetof(R, src_index)   == 1,  "R.src_index");
_Static_assert(offsetof(R, dst_type)    == 2,  "R.dst_type");
_Static_assert(offsetof(R, dst_channel) == 3,  "R.dst_channel");
_Static_assert(offsetof(R, op)          == 4,  "R.op");
_Static_assert(offsetof(R, flags)       == 5,  "R.flags");
_Static_assert(offsetof(R, param_idx)   == 6,  "R.param_idx");
_Static_assert(offsetof(R, state_offset)== 8,  "R.state_offset");
_Static_assert(offsetof(R, actuator_idx)== 10, "R.actuator_idx");
_Static_assert(offsetof(R, wire2_idx)   == 12, "R.wire2_idx");
_Static_assert(offsetof(R, period)      == 14, "R.period");
_Static_assert(offsetof(R, reserved)    == 15, "R.reserved");

int main(void) { return 0; }
