# N-A 审计复现: RS 复位端引用被钉到 wire[0] 的信号
# 修复前: pack_routes 用 wire2 值设 WIRE2 标志, 引 wire[0] 时值=0 → 丢标志
#   → 固件判"无第二源" → R 端静默失效 → RS(S1=1, R=1) 复位优先应出 0 却出 1
# 修复后: wire2_valid 显式布尔 → 标志带上 → R=1 生效 → latch = 0
CONST   one     = 1.0
OUTPUT  pinned  TO wire[0] FROM one    # 把 1 钉到 wire[0] (显式固定, 触发哨兵边界)
RS      latch   S1=one  R=pinned       # S1=1 R=1 → 复位优先应输出 0
