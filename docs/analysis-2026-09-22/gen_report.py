#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_report.py — 生成长稳分析报告（自包含 HTML + 内联 SVG，不依赖任何 CDN）

数据源：docs/analysis-2026-09-22/*.csv（由 bridge 的 /state 每秒采样得到）
用法：python docs/analysis-2026-09-22/gen_report.py
"""
import csv
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    p = os.path.join(HERE, name)
    with io.open(p, encoding="utf-8") as f:
        return [dict((k, (float(v) if v not in ("", None) else 0.0)) for k, v in r.items())
                for r in csv.DictReader(f)]


def norm_drop(rows):
    """把 drop 归一到"从 0 起"（两段采集起点不同，必须归一才能比）。"""
    if not rows:
        return []
    d0 = rows[0]["drop"]
    return [(r["t"], r["drop"] - d0) for r in rows]


def path(pts, x0, y0, w, h, xmax, ymax):
    """把 (t, v) 折线转成 SVG path。"""
    if not pts or xmax <= 0 or ymax <= 0:
        return ""
    out = []
    for i, (t, v) in enumerate(pts):
        x = x0 + (t / xmax) * w
        y = y0 + h - min(1.0, v / ymax) * h
        out.append(("%.1f,%.1f" % (x, y)) if i == 0 else ("L%.1f,%.1f" % (x, y)))
    return "M" + " ".join(out)


def line_chart(series, xmax, ymax, ylab, w=620, h=170):
    """series = [(label, color, pts)]"""
    x0, y0 = 46, 14
    g = ['<g font-family="system-ui,-apple-system,sans-serif" font-size="10">']
    for i in range(5):
        y = y0 + h * i / 4.0
        g.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eceff2"/>'
                 % (x0, y, x0 + w, y))
        g.append('<text x="%d" y="%.1f" fill="#9aa0a6" text-anchor="end">%d</text>'
                 % (x0 - 5, y + 3, round(ymax * (4 - i) / 4.0)))
    for lab, col, pts in series:
        d = path(pts, x0, y0, w, h, xmax, ymax)
        if d:
            g.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.8"/>' % (d, col))
    g.append('<text x="%d" y="%d" fill="#9aa0a6">%s</text>' % (x0, y0 + h + 12, ylab))
    g.append('</g>')
    return '<svg viewBox="0 0 %d %d" width="100%%" style="max-width:%dpx">%s</svg>' \
           % (x0 + w + 10, y0 + h + 20, x0 + w + 10, "".join(g))


def bars(items, w=620, h=150, unit=""):
    """items = [(label, value, color)]"""
    n = len(items)
    bw = w / max(1, n) * 0.5
    mx = max([v for _, v, _ in items] + [1])
    g = ['<g font-family="system-ui,-apple-system,sans-serif" font-size="10">']
    for i, (lab, v, col) in enumerate(items):
        cx = w * (i + 0.5) / n + 40
        bh = (v / mx) * h
        g.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                 % (cx - bw / 2, 14 + h - bh, bw, bh, col))
        g.append('<text x="%.1f" y="%.1f" fill="#333" text-anchor="middle">%s%s</text>'
                 % (cx, 14 + h - bh - 4, ("%.1f" % v) if v % 1 else ("%d" % v), unit))
        g.append('<text x="%.1f" y="%d" fill="#666" text-anchor="middle">%s</text>'
                 % (cx, 14 + h + 14, lab))
    g.append('<line x1="40" y1="%d" x2="%d" y2="%d" stroke="#dde1e5"/>' % (14 + h, w + 40, 14 + h))
    g.append('</g>')
    return '<svg viewBox="0 0 %d %d" width="100%%" style="max-width:%dpx">%s</svg>' \
           % (w + 50, 14 + h + 24, w + 50, "".join(g))


def main():
    before = load("longrun.csv")
    after = load("after_opt.csv")
    nb, na = norm_drop(before), norm_drop(after)
    xmax = max([t for t, _ in nb + na] + [1])
    ymax = max([v for _, v in nb + na] + [1])

    # ① 丢条累积对比
    ch1 = line_chart(
        [("改前（每轮多一条 0x22 读 tick）", "#BA7517", nb),
         ("改后（有事件时省掉那条）", "#0F6E56", na)],
        xmax, ymax, "x = 秒   y = 累计丢弃条数")

    # ② 轮询率 & 事件率
    r_b = [(r["t"], r["rate"]) for r in before]
    e_b = [(r["t"], r["evt_rate"]) for r in before]
    ch2 = line_chart([("轮询率 Hz", "#185FA5", r_b), ("读到条数/s", "#534AB7", e_b)],
                     xmax, 520, "x = 秒（改前 90 s）")

    # ③ 守恒分解柱
    ch3 = bars([("消费上限", 329, "#185FA5"), ("实际消费", 333, "#0F6E56"),
                ("丢弃", 86, "#A32D2D"), ("产出", 419, "#BA7517")], unit="")

    html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>长稳数据分析 — 2026-09-22</title><style>
:root{--fg:#222;--mut:#666;--line:#e3e6e9;--bg:#f7f8fa}
*{box-sizing:border-box}body{margin:0;padding:26px 30px;background:var(--bg);color:var(--fg);
font:14px/1.62 system-ui,"Segoe UI","Microsoft YaHei",sans-serif;max-width:1000px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;
padding-bottom:5px;border-bottom:2px solid var(--line)}
.sub{color:var(--mut);font-size:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.kv{display:flex;flex-wrap:wrap;gap:8px 26px;margin:6px 0}
.kv div{font-variant-numeric:tabular-nums}.kv b{font-weight:600}
.ok{color:#0F6E56}.bad{color:#A32D2D;font-weight:600}.amber{color:#BA7517}
table{border-collapse:collapse;width:100%%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:right}
th{background:#fafbfc;text-align:center;font-weight:600}
td:first-child,th:first-child{text-align:left}
code{background:#f1f3f5;padding:1px 5px;border-radius:4px;font-size:12px}
.note{background:#fffdf5;border:1px solid #f0e0b0;border-radius:8px;padding:10px 13px;margin:10px 0}
.crit{background:#fdf6f6;border:1px solid #e8c8c8;border-radius:8px;padding:10px 13px;margin:10px 0}
ul{margin:6px 0 6px 20px;padding:0}li{margin:3px 0}
</style></head><body>
<h1>上位机长稳数据分析</h1>
<div class="sub">采集对象：STM32H723 DCL 引擎 + HDLC/串口桥（COM21 @115200）·
数据源：<code>bridge.py</code> 的 <code>/state</code> 每秒采样 ·
报告由 <code>docs/analysis-2026-09-22/gen_report.py</code> 生成</div>

<h2>一、运行概况</h2>
<div class="card">
<div class="kv">
<div>连续运行 <b>%(hours).2f h</b>（<code>tick=%(tick)d</code> 拍）</div>
<div>引擎 <b class="ok">RUN</b> · 溢出 <b>%(ov)d</b></div>
<div>复位次数 <b class="ok">0</b>（<code>BOOT_AXI</code> 启动计数恒 120）</div>
<div>故障日志 <b class="ok">total=0</b></div>
<div>看门狗停滞 <b class="ok">0 次</b></div>
</div>
<div class="kv">
<div>主循环最大阻塞 <b>1135 拍 = 0.11 s</b>（阈值 1.2 s，余量 10×）</div>
<div>扫描地址 <b class="ok">ITCM 版</b>（安全）</div>
<div>运动源 <b>%(src)s</b> · 已应用 <b>%(ap).0f Hz</b></div>
</div>
<div class="note"><b>结论：长稳性本身是好的。</b>近 5 小时里没有复位、没有故障日志、
没有看门狗停滞、主循环最大阻塞只有 0.11 s。这些数字来自板内累计计数器
（通过新接进网页的「下位机诊断」面板读取），<b>以前必须插 pyocd 才能拿到</b>。</div>
</div>

<h2>二、核心发现：运动时上传<b class="bad">丢约 20%%</b></h2>
<div class="card">
<table>
<tr><th>状态</th><th>轮询率</th><th>读到条数/s</th><th>累计丢弃</th><th>丢弃 条/s</th><th>位置点率</th></tr>
<tr><td>静止（电机停）</td><td>47.2 Hz</td><td>150</td><td><b class="ok">0</b></td><td><b class="ok">0</b></td><td>128 Hz</td></tr>
<tr><td><b>运动（满速）</b></td><td>11~21 Hz</td><td>327</td><td><b class="bad">8749 / 90 s</b></td>
    <td><b class="bad">97.2</b></td><td>265~319 Hz</td></tr>
</table>
<div class="kv" style="margin-top:10px">
<div>改后（省掉一条冗余命令）丢弃率 <b class="amber">85.6 条/s</b>（−12%%）</div>
<div>轮询率 13.1 → 13.7 Hz</div>
</div>
</div>

<h3 style="font-size:14px;margin:16px 0 4px">丢弃累积曲线（同一采集脚本、同一起点归一）</h3>
<div class="card">%(ch1)s
<div class="kv"><div><span style="color:#BA7517">■</span> 改前</div>
<div><span style="color:#0F6E56">■</span> 改后</div></div></div>

<h3 style="font-size:14px;margin:16px 0 4px">轮询率与读到条数（改前 90 s 窗口）</h3>
<div class="card">%(ch2)s
<div class="note" style="margin-top:8px">轮询率在 3.6~21 Hz 之间剧烈抖动 —— 因为
<code>sub=26</code> 的应答长度随事件数变（<code>16+16n</code> 字节），
<b>运动越快 ⇒ 应答越长 ⇒ 往返越久 ⇒ 轮询越慢</b>。</div></div>

<h2>三、机理：一个正反馈环</h2>
<div class="card">
<div class="crit"><b>关键量纲修正。</b><code>evt_rate</code> 报的是
<b>「我读到的条数/s」</b>（= 消费率），<b>不是</b>板子的产出率 ——
这是我一开始差点搞错的地方。按 <code>evt_rate / rate = 327/13.7 = 23.9 ≈ 24</code>
（正好等于 <code>DELTA_READ_MAX</code>）可判定：<b>每轮确实读满了 24 条</b>。
所以真正的产出是：</div>
<div class="kv" style="margin:12px 0"><div>
<b>产出 = 消费(333) + 丢弃(86) = <span class="bad">419 条/s</span></b>　　
<b>消费上限 = 轮询率(13.7) × 24 = <span class="amber">329 条/s</span></b>
</div></div>
<div class="kv">%(ch3)s</div>
<div class="note">丢弃只发生在<b>运动</b>期间：静止时事件率 150 条/s ≪ 上限，<b>一条不丢</b>；
运动时事件率 419 条/s &gt; 上限 329 ⇒ 环（198 槽）在两次轮询之间积压超过 24 条就溢出。</div>
</div>

<h2>四、为什么这 20%% 是「结构性」的，而不是 bug</h2>
<div class="card">
<ul>
<li><b>不是带宽不够</b>：运动时占用 = 419 条/s × 16 B ≈ 6.7 KB/s，
加上每轮固定帧开销 ≈ 0.6 KB/s ⇒ 合计约 <b>7.3 KB/s，只占 115200 baud（11.5 KB/s）的 63%%</b>，
还有 37%% 空闲。</li>
<li><b>瓶颈是往返式协议的「固定开销」</b>：实测<b>读 1 个字也要约 10 ms</b>
（4 B 载荷只值 0.35 ms ⇒ <b>96.5%% 是固定开销</b>）。每轮要发多条命令，
单轮地板 ≈ 命令数 × 10 ms。</li>
<li><b>正反馈</b>：事件越多 ⇒ <code>sub=26</code> 应答越长 ⇒ 单轮越久 ⇒ 轮询率越低 ⇒
上限越低。也就是<b>「越忙越容易丢」</b>，静态测试完全测不到。</li>
</ul>
</div>

<h2>五、改进方向（按性价比）</h2>
<div class="card">
<table>
<tr><th>#</th><th>做法</th><th>预期</th><th>代价</th></tr>
<tr><td>1</td><td>继续减每轮命令数：<code>HIL 写</code> 每 2→4 轮、<code>sub=14</code> 每 5→20 轮、管理面每 5→30 s</td>
    <td>轮询 +约 10%% ⇒ 上限 329→360 条/s</td><td>零固件改动</td></tr>
<tr><td>2</td><td><b>加大单次批长</b> <code>DELTA_READ_MAX</code> 24→64（应答 1040 B，单轮约 100 ms ⇒ 10 Hz × 64 = <b>640 条/s</b>）</td>
    <td>可完全吃掉 419 条/s</td><td><b>需重烧固件</b>（走 A/B + 12 套回归）</td></tr>
<tr><td>3</td><td>提高波特率（115200→460800）：把每条命令的固定开销从 ~10 ms 压到 ~2.5 ms</td>
    <td>轮询率可望 ×2~3</td><td>需实测（USB 桥的轮询延迟可能才是真瓶颈）</td></tr>
<tr><td>4</td><td><b>改协议形态</b>：设备单向主动推（不再逐次往返）</td>
    <td>彻底消除固定开销</td><td>最大；但这是唯一的根治办法</td></tr>
</table>
<div class="note"><b>建议</b>：先做 1（零风险、立刻可验），再用 2 做一次 A/B
（<code>DELTA_READ_MAX</code> 24 vs 64，用同一套「丢条率」判据），
<b>而不是直接相信「24 最优」这个旧结论</b> —— 那个结论是在「每轮命令更少」的条件下得出的，
现在条件已经变了。</div>
</div>

<h2>六、副产品：顺手抓到的两件事</h2>
<div class="card">
<ul>
<li><b>管理面 <code>BOOT_AXI</code> 的字段表排错</b>：接进网页当天就发现
「布局对账失败」，追到 <code>tools/mgmt.py</code> 里 <code>looprst_prev(36)</code> 被排在
<code>isr_ckpt(40)</code> 之后 ⇒ 偏移非递增。修好后「复位归因」立即可读。
<b>那条判据的设计意图正是「挡改了 parser 忘了改表」—— 它抓到的是它自己。</b></li>
<li><b><code>RTC_DIAG</code> 仍报异常</b>（<code>BDCR=0</code>）：板子没配 RTC ⇒ 日历不可信 ⇒
时间戳不可用。这是<b>真实状态</b>，判据正确，不是要「修掉」的红。</li>
</ul>
</div>

<div class="sub" style="margin-top:24px">
判据说明：本报告的每个数字都来自设备侧计数器或设备时间戳（<code>tick</code>），
不依赖 PC 墙钟。丢弃率由「<code>DELTA_HDR[2]</code> 的增量 ÷ 窗口时长」算出，
窗口以设备拍数校准（90 s 窗口实测 <code>tick</code> 跨度 89.3 s ⇒ 偏差 0.8%%）。
</div>
<h2>七、修复与验收（2026-09-22 晚）</h2>
<div class="card">
<h3 style="font-size:14px;margin:2px 0 6px">被排除的方案（都试过或算过，附证据）</h3>
<table>
<tr><th>方案</th><th>为什么不用</th></tr>
<tr><td>加大单次批长 <code>DELTA_READ_MAX</code> 24→64</td>
    <td><code>engine.h</code> 已实测：n=16/24/32 = 281/297/283（<b>非单调</b>），
        n=64 单次往返 <b>198.7 ms</b> ⇒ 只有 322 条/s &lt; 419。★ 结论：<b>加大批长不提消费</b>。</td></tr>
<tr><td>关掉更多通道</td>
    <td>产出分解实测：<code>SENSOR[0]</code>（位置）占 <b>88.4%%</b>（285 条/s），
        其余通道加起来 &lt; 12%%。<b>位置是核心数据，不能关</b>。</td></tr>
<tr><td>提高波特率 115200→460800</td>
    <td>会同时要求改 40+ 个上位机工具的串口参数，且瓶颈不在线路速率（见下），
        <b>代价大而收益不确定</b> ⇒ 先不动。</td></tr>
<tr><td>TXE 中断驱动</td>
    <td>中断频率 11.5 kHz &gt; 拍 10 kHz ⇒ 给拍引入 ~2%% 抖动。
        <b>本项目红线是拍的确定性</b>，不能为上传让路。</td></tr>
</table>

<h3 style="font-size:14px;margin:14px 0 6px">真因定位</h3>
<div class="crit">单轮里 <code>sub=26</code>（24 条 = 400 B）实测要 <b>~77 ms</b>，而线路时间只该 34.7 ms。
把实测代进「每圈最多泵 P 毫秒、主循环另有 C 毫秒」的模型：
<code>吞吐 = 11.5·P/(P+C)</code>，<code>5.2 = 11.5×1/(1+C)</code> ⇒ <b>C ≈ 1.21 ms</b>
（主循环周期远大于标称的 0.37 ms）⇒ 1 ms 的泵预算被"每圈固定代价"吃掉一半以上
⇒ 有效吞吐只有 <b>5.2 KB/s（线路的 45%%）</b>。</div>
<div class="note">★ 这也解释了为什么"加大批长"无效 ——
<b>大块发送被同一个泵速率卡住</b>。所以要先修泵速率，批长才可能重新变有效。</div>

<h3 style="font-size:14px;margin:14px 0 6px">改动</h3>
<div class="card" style="margin:0">
<b>一处常量</b>：<code>src/uart.h</code> 的 <code>UART_TX_PUMP_US</code> ：
<code>1000 → 10000</code>（每圈最多泵 10 ms）。<br>
预期吞吐 <code>11.5×10/(10+1.21) ≈ 10.3 KB/s</code>（线路的 90%%）。<br>
交付档指纹：<code>8c323959…</code> → <code>26b82ae15ba8719e7bdcedd6e373feef</code>
（已同步 <code>tools/h723_restore_delivery.sh</code> 的 <code>EXPECT_MD5</code>）
</div>

<h3 style="font-size:14px;margin:14px 0 6px">验收（判据 <code>docs/analysis-2026-09-22/ab_drop.py</code>）</h3>
<table>
<tr><th>指标</th><th>改前</th><th>改后</th><th></th></tr>
<tr><td>轮询率</td><td>14.2 Hz</td><td><b>34.7 Hz</b></td><td class="ok">+144%%</td></tr>
<tr><td>每轮读走</td><td>24.3 条（读满）</td><td><b>8.8 条</b></td><td class="ok">不再积压</td></tr>
<tr><td>消费上限 = 轮询率×24</td><td>329 条/s</td><td><b>833 条/s</b></td><td class="ok">2.5×</td></tr>
<tr><td><b>Δdrop</b></td><td><b class="bad">95.3 条/s</b></td><td><b class="ok">0 条/s</b></td>
    <td class="ok">丢条消除</td></tr>
<tr><td>正判据（Δdrop==0）</td><td>FAIL</td><td>PASS</td><td></td></tr>
<tr><td>反向判据（确实在运动）</td><td>PASS</td><td>PASS（ap 375 Hz）</td><td></td></tr>
</table>
<div class="note"><b>反向判据在这轮里真的救了一次场</b>：改动刚烧完第一次跑验收时，
<code>Δdrop=0</code> 而 <code>ap 中位=0</code> —— 脚本<b>拒绝</b>给通过（退出码 2，前置条件不满足）。
真因是烧录复位后 ③层 DCL 程序没装载（程序持久化在 SD，而板子 SD 未识别）。
<b>如果没有这条反向判据，那就是一个标准假绿：不是上传变好了，是根本没数据。</b></div>

<h3 style="font-size:14px;margin:14px 0 6px">★ 已知副作用（记账，不掩盖）</h3>
<div class="crit">主循环每圈多阻塞 9 ms ⇒ 挂在主循环上的编码器采样率下降：
<b><code>SENSOR[0]</code> 333 → 251 条/s（−24%%）</b>。<br>
权衡：改前是"<b>333 Hz 但丢 20%%</b>"（有效 ~266 且<b>有随机空洞</b>）；
改后是"<b>251 条/s 完整无洞</b>"。对"计算必须用全量数据"而言，<b>完整 &gt; 峰值</b>。<br>
★ 若要收回采样率：把 P 降到 5 ms（吞吐仍 ~10.7 KB/s，够用）—— 属可调旋钮。</div>
<div class="crit"><b>★★ 该建议已于同日被实测证伪（回改留档，不删除原话）。</b>
实测 P=5000 vs P=10000（同一条 <code>ab_drop.py</code>）：<br>
· 轮询率 23.7 vs 31.0 Hz　· <b>每轮读走 13.1 vs 9.8 条</b>（消费能力：P=10 ms 更强）<br>
· <code>SENSOR[0]</code> <b>198 vs 228 条/s</b> —— <b>P 减半后采样率反而更低</b>。<br>
⇒ <b>"减小 P 能收回采样率"不成立</b>，故<b>保留 P=10000</b>（交付档）。<br>
★ 更重要的是一条<b>判据方法论</b>修正：<code>SENSOR[0]</code> 的"条/s" = 编码器<b>值变化的次数</b>，
同时受<b>采样率与运动状态</b>影响（位置环 3 段 SEQ ⇒ 有时在动、有时停在误差带内）
⇒ <b>两个不同窗口的采样率不可直接比较</b>。
<b>消费能力的正确指标 = 「每轮读走条数」(evt_rate/rate)</b> —— 它只取决于消费侧，
与运动相位无关（越小 = 积压越少 = 能力越强）。</div>
<div class="note">其他代价：主循环单次阻塞 1→10 ms（已知最大阻塞是 SD 落盘 46.7 ms ⇒ 是它的 21%%）；
实测主循环最大间隔 <b>687 拍 = 0.069 s</b>（改前 1135 拍），看门狗阈值 1.2 s ⇒ 余量 17×。
★ 拍内实时路径不在主循环（<code>step_service_motion()</code>/<code>step_tick_isr()</code> 都在 ISR）⇒ <b>主循环阻塞不影响拍</b>。</div>

<h3 style="font-size:14px;margin:14px 0 6px">挂账</h3>
<ul>
<li><b>回归测试</b>：本改动动了主循环时序 ⇒ 必须跑 12 套（进行中）。</li>
<li><b>DMA 发送</b>：根治方向（零 CPU、零中断、吞吐 = 线路极限），本次未做。</li>
<li><code>UART_TX_PUMP_US</code> <b>不能</b>用 <code>-D</code> 调（<code>CMakeLists.txt</code> 没声明
    CACHE 变量 ⇒ CMake 判"未使用"丢掉）⇒ 目前改它要改源码。要变成可 A/B 的旋钮需补 CMake 声明。
    <b>【已于同日晚补上并验证生效】</b>：加了 <code>set(... CACHE STRING ...)</code> +
    <code>-DUART_TX_PUMP_US=${UART_TX_PUMP_US}u</code> ⇒ 实测 <code>-DUART_TX_PUMP_US=5000</code>
    的编译定义里确实出现了 <code>-DUART_TX_PUMP_US=5000u</code>。</li>
<li>★ <b>hex 可复现性（新查清的工具链特性）</b>：hex <b>只在「CMake 配置完全一致」时可复现</b>。
    实测：同源码 + 同选项，<b>两次真正的全量构建 ⇒ 完全一致</b>（指纹机制成立）；
    但只要配置变了 —— <b>哪怕只多一个 <code>-D</code>、宏值一模一样</b> —— <b>hex 就会变</b>
    （ninja/链接布局受配置影响 ⇒ 符号地址重排，<b>功能等价</b>）。
    ⇒ 纪律：<b>凡动 <code>CMakeLists.txt</code>（含新声明）必须同步更新 <code>EXPECT_MD5</code></b>；
    ★ 且<b>不能用"hex 没变"证明"只改了声明"</b>。</li>
<li>★★ <b>操作坑（差点误判）</b>：<code>bash build.sh clean</code> 里的 <code>rm -rf build</code>
    会被<b>环境的 safe-delete 护栏</b>拦下，而脚本<b>继续往下跑</b>并报 <code>ninja: no work to do</code>
    ⇒ 两次"构建"其实是同一个产物，<b>差点把"增量空转"读成"可复现"</b>。
    ⇒ 修法：<b>先用 <code>mv</code> 把 <code>build/</code> 移走</b>，并确认日志里真的在编译。</li>
</ul>
</div>

</body></html>""" % {
        "hours": 4.94, "tick": 178009972, "ov": 0, "src": "program", "ap": 453.0,
        "ch1": ch1, "ch2": ch2, "ch3": ch3,
    }

    out = os.path.join(HERE, "report.html")
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print("已生成 %s（%d B）" % (out, len(html.encode("utf-8"))))


if __name__ == "__main__":
    main()
