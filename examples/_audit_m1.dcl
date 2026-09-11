# _audit_m1.dcl — 审计 M1 复现: AND/OR 引用首个分配信号不再恒假
# 修复前: auto_next=0 → 首信号占 wire[0] → wire2_idx=0 被固件当"无第二源"
#         → AND 恒假 (无编译错误/NAK, 静默失效)
# 修复后: auto_next=1 → 首信号占 wire[1] → wire2_idx 永非 0 → 逻辑正确
# 判定: ok1=1 (a1 AND a1), ok0=0 (a1 AND a0)

SENSOR  fb      FROM sensor[2]            # 反馈 (H723 闭环中 ≈57)
SCALE   fb_cal  FROM fb  K=1.0  B=0.0     # 直通
ALARM   a1      FROM fb_cal > 20          # 稳态 57>20 → 1
ALARM   a0      FROM fb_cal > 90          # 稳态 57<90 → 0
LOGIC   ok1     = a1 AND a1               # 期望 1 (第二输入 = 首个信号 a1)
LOGIC   ok0     = a1 AND a0               # 期望 0
