# OA1 审计复现: CTU 无 R 端但 wire[0] 被钉 1.0 → 恒清零
# 修复前 (N-A 反向尾巴): 无 R 端时 w2=0 → wire2_valid=True → 误带 WIRE2 标志
#   → 固件把 wire[0](=1.0) 当 CTU 复位端 → 计数被逐拍清零 (cnt 恒 0)
# 修复后 (else None): 无 R 端 = 无第二输入, 不带标志 → cnt 正常计数 = 1
CONST   one     = 1.0
OUTPUT  pinned  TO wire[0] FROM one    # 钉 1.0 到 wire[0] (触发边界)
CTU     cnt     CU=one PV=5            # 无 R 端, 一次上升沿 → CV 应 = 1
