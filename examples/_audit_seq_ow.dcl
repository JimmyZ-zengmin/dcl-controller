# 审计 OA3 复现 (第十五轮): SEQ out_wire 与 auto 分配路由槽撞车 → 双写者
# 30 个 SENSOR auto 分配占 wire[1..30], 再 SEQ TO wire[30] — 修复前 dclc 静默放行,
# 固件也放行 → wire[30] 步号镜像被 SENSOR 路由每拍覆盖 (静默失效, 实测全 30.0)
# 修复后: 编译期就该报错 (双写者), 不会生成任何下发帧
SENSOR  s01  FROM sensor[3]
SENSOR  s02  FROM sensor[4]
SENSOR  s03  FROM sensor[5]
SENSOR  s04  FROM sensor[6]
SENSOR  s05  FROM sensor[3]
SENSOR  s06  FROM sensor[4]
SENSOR  s07  FROM sensor[5]
SENSOR  s08  FROM sensor[6]
SENSOR  s09  FROM sensor[3]
SENSOR  s10  FROM sensor[4]
SENSOR  s11  FROM sensor[5]
SENSOR  s12  FROM sensor[6]
SENSOR  s13  FROM sensor[3]
SENSOR  s14  FROM sensor[4]
SENSOR  s15  FROM sensor[5]
SENSOR  s16  FROM sensor[6]
SENSOR  s17  FROM sensor[3]
SENSOR  s18  FROM sensor[4]
SENSOR  s19  FROM sensor[5]
SENSOR  s20  FROM sensor[6]
SENSOR  s21  FROM sensor[3]
SENSOR  s22  FROM sensor[4]
SENSOR  s23  FROM sensor[5]
SENSOR  s24  FROM sensor[6]
SENSOR  s25  FROM sensor[3]
SENSOR  s26  FROM sensor[4]
SENSOR  s27  FROM sensor[5]
SENSOR  s28  FROM sensor[6]
SENSOR  s29  FROM sensor[3]
SENSOR  s30  FROM sensor[4]      # auto → wire[30]
SEQ  cycle  TO wire[30]
    DWELL  1s
    DWELL  1s
    LOOP
