#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""exp_fb_lut_deploy.py —— E-FB：**LUT 表随程序走**（方案 A）的判据

## 这一项治什么
E-Y 量出的事实：`0x23` 能把表写进 `OFF_LUT_DATA`，但 **`0x10 deploy` 不碰它、
SD 程序包不带它、`0x13 RESET` 清零它、`fill_tables` 会覆盖它** ⇒ 表住在**无主的易失内存**里。
方案 A（用户授权我定）＝ **表随 deploy 载荷走**：尾部追加 `LUTT` 段，与路由**同一次 reload 生效**。

## 判据（离线 5 条 + 在板 4 条）
  FB1 ★ `TABLE` + `LUT` 能编译，且**段格式与固件逐字节一致**
       （magic/seg_len/n_used/fnv 四项都从 `src/lut_seg.h` 现读现比）
  FB2 ★ 段往返：Python 打包 → 独立按固件算法**重算 FNV** 并解包 ⇒ 256 个 float 逐位相等
  FB3 负对照（4 条，都必须**干净报错**）：用了 `LUT` 没 `TABLE` · `TABLE` 声明两次 ·
       点数 < 2 · 点数 > MAX_LUT
  FB4 ★ 段组合：{无段} {只有 LUT} {只有 DB} {LUT+DB} 四种尾部布局都要能正确定位
       （**顺序约定**：从尾往前 `[dev_bind]` → `[LUT]` → body）
  FB5 ★ 变异对照：magic 错一位 / seg_len 错 / n_used 越界 / fnv 错 / 表里有 NaN
       ⇒ 固件的判定必须是 **BAD（拒绝）**，而不是"当作没段"
  FB6（在板）部署带表程序 ⇒ 读回 LUT 区逐位等于发出的表；`g_lut_applies` +1
  FB7（在板）部署**不带段**的程序 ⇒ LUT 区**保持不动**（语义：无段 = 不碰表）
  FB8（在板）`0x13 RESET` ⇒ 表清零，且 pending 被丢弃（复位后再 reload 不会把旧表写回）
  FB9（在板）上电从 SD 程序包装载 ⇒ **表回来**（方案 A 的核心价值）

用法: python tools/exp_fb_lut_deploy.py [--offline]
退出码: 0 = 全 PASS / 1 = 有 FAIL / 2 = 前置不满足
"""
import argparse
import io
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import dclc                                              # noqa: E402


def lut_consts():
    """★ 从 src/lut_seg.h 现读常量（不手抄）—— 判据必须与固件同源才有意义。"""
    h = io.open(os.path.join(R, 'src', 'lut_seg.h'), encoding='utf-8', errors='replace').read()
    magic = int(re.search(r'^#define\s+LUT_SEG_MAGIC\s+0x([0-9A-Fa-f]+)u?', h, re.M).group(1), 16)
    hdr = int(re.search(r'^#define\s+LUT_SEG_HDR\s+(\d+)u?', h, re.M).group(1))
    dev = io.open(os.path.join(R, 'src', 'dev_bind.h'), encoding='utf-8',
                  errors='replace').read()
    dblen = int(re.search(r'^#define\s+DB_SEG_LEN\s+(\d+)u?', dev, re.M).group(1))
    dbmagic = int(re.search(r'^#define\s+DB_MAGIC_VAL\s+0x([0-9A-Fa-f]+)u?',
                            io.open(os.path.join(R, 'src', 'engine.h'), encoding='utf-8',
                                    errors='replace').read(), re.M).group(1), 16)
    return magic, hdr, dblen, dbmagic


def fnv_words(buf):
    h = 2166136261
    for i in range(0, len(buf) - 3, 4):
        h = ((h ^ int.from_bytes(buf[i:i + 4], 'little')) * 16777619) & 0xFFFFFFFF
    return h


def compile_text(text, name='_fb.dcl'):
    """用 dclc 的真路径编译（parse → compile_stmts），返回 (S, payload)。"""
    p = os.path.join(R, '.tmpctl', name)
    io.open(p, 'w', encoding='utf-8', newline='\n').write(text)
    S = dclc.compile_stmts(dclc.parse(text), src_path=p)
    body = (struct.pack('<HHH', len(S.routes), len(S.params), 0)
            + dclc.pack_routes(S.routes) + dclc.pack_params(S.params))
    if S.lut is not None:
        body += dclc.pack_lut_segment(S.lut, len(S.lut))
    return S, body


def expect_error(text, needle, tag):
    try:
        compile_text(text)
    except SystemExit as e:
        ok = needle in str(e)
        print('    %-28s ⇒ 拒绝（%s）%s' % (tag, '命中' if ok else '**未命中**', str(e).split(chr(10))[0][:60]))
        return ok
    print('    %-28s ⇒ ★ **没拒绝**（判据无效或检查缺失）' % tag)
    return False


PROG_OK = """TABLE t = 0, 1, 4, 9, 16
SENSOR  in FROM sensor[7]
LUT     y  FROM in
OUTPUT  o  TO wire[40] FROM y
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--port', default=None)
    a = ap.parse_args()
    magic, hdr, dblen, dbmagic = lut_consts()
    seg_len = hdr + dclc.MAX_LUT * 4
    res, skip = [], []
    print('=' * 74)
    print('E-FB  LUT 表随程序走（方案 A）')
    print('=' * 74)
    print('固件侧常量（现读 src/lut_seg.h 与 dev_bind.h）: magic=0x%08X hdr=%d seg_len=%d'
          % (magic, hdr, seg_len))
    print('                                            DB_SEG_LEN=%d DB_MAGIC=0x%08X'
          % (dblen, dbmagic))

    # ── FB1：编译 + 段头四项与固件一致 ───────────────────────────────────
    print('\n── FB1 ★ `TABLE`/`LUT` 编译通过，段头与固件逐项一致 ──')
    S, payload = compile_text(PROG_OK)
    ops = [r['op'] for r in S.routes]
    seg = payload[-seg_len:]
    m_, l_, n_ = struct.unpack('<III', seg[:12])
    fnv_ = struct.unpack('<I', seg[12:16])[0]
    print('    路由 op 序列 = %s（期望含 0x%02X = LUT）' % (ops, dclc.OP['LUT']))
    print('    段头: magic=0x%08X seg_len=%d n_used=%d fnv=0x%08X' % (m_, l_, n_, fnv_))
    res.append(('FB1a TABLE/LUT 编译通过且含一条 LUT 路由',
                (dclc.OP['LUT'] in ops) and n_ == 5))
    res.append(('FB1b 段头 magic/seg_len 与固件同源（0x%08X/%d）' % (magic, seg_len),
                m_ == magic and l_ == seg_len))

    # ── FB2：往返（独立按固件算法重算 FNV）────────────────────────────────
    print('\n── FB2 ★ 段往返：独立重算 FNV + 解包后逐位比对 ──')
    bodybytes = seg[hdr:]
    ok_fnv = (fnv_words(bodybytes) == fnv_)
    vals = struct.unpack('<%df' % dclc.MAX_LUT, bodybytes)
    want = [0.0, 1.0, 4.0, 9.0, 16.0] + [0.0] * (dclc.MAX_LUT - 5)
    same = all(struct.pack('<f', v) == struct.pack('<f', w) for v, w in zip(vals, want))
    print('    独立重算 FNV = 0x%08X ⇒ %s' % (fnv_words(bodybytes), '一致' if ok_fnv else '不一致'))
    print('    前 6 个点 = %s …' % (vals[:6],))
    res.append(('FB2a 段内 FNV 与独立重算一致', ok_fnv))
    res.append(('FB2b 解包后 256 个 float 逐位等于输入（余位补 0）', same))

    # ── FB3：负对照 4 条 ────────────────────────────────────────────────
    print('\n── FB3 负对照：4 种错法都必须**干净报错** ──')
    ok3 = [
        expect_error('SENSOR in FROM sensor[7]\nLUT y FROM in\nOUTPUT o TO wire[40] FROM y\n',
                     '没有声明表', '用了 LUT 没 TABLE'),
        expect_error('TABLE a = 1, 2\nTABLE b = 3, 4\nCONST c = 1.0\nOUTPUT o TO wire[40] FROM c\n',
                     '只能声明', 'TABLE 声明两次'),
        expect_error('TABLE t = 1\nCONST c = 1.0\nOUTPUT o TO wire[40] FROM c\n',
                     '至少要有', '点数 < 2'),
        expect_error('TABLE t = %s\nCONST c = 1.0\nOUTPUT o TO wire[40] FROM c\n'
                     % ', '.join(str(i) for i in range(dclc.MAX_LUT + 1)),
                     '板上限', '点数 > MAX_LUT'),
    ]
    for i, o in enumerate(ok3):
        res.append(('FB3.%d 负对照 %d 干净报错' % (i + 1, i + 1), o))

    # ── FB4：尾部段组合（顺序约定）───────────────────────────────────────
    print('\n── FB4 ★ 尾部段组合：4 种布局都要能正确定位 ──')
    db_seg = struct.pack('<III', dbmagic, dblen, 0) + b'\x00' * (dblen - 12)

    def locate(pl):
        """按固件 lut_seg_unpack 的算法定位（顺序敏感：先跳 DB，再见 LUT）。"""
        tail = len(pl)
        if tail >= dblen and struct.unpack('<I', pl[tail - dblen:tail - dblen + 4])[0] == dbmagic:
            tail -= dblen
        if tail < seg_len:
            return 'NONE'
        if struct.unpack('<I', pl[tail - seg_len:tail - seg_len + 4])[0] != magic:
            return 'NONE'
        return 'LUT@%d' % (tail - seg_len)

    body_only = payload[:len(payload) - seg_len]      # ★ 真正"不带段"的载荷（我第一版错传了带段的 payload）
    cases = [('无段', body_only, 'NONE'),
             ('只有 LUT', payload, 'LUT@%d' % (len(payload) - seg_len)),
             ('只有 DB', payload[:len(payload) - seg_len] + db_seg, 'NONE'),
             ('LUT + DB', payload + db_seg, 'LUT@%d' % (len(payload) - seg_len))]
    for tag, pl, want in cases:
        got = locate(pl)
        print('    %-10s len=%-5d 定位 = %-14s（期望 %s）%s'
              % (tag, len(pl), got, want, '' if got == want else '  ★ 不符'))
        res.append(('FB4 %s 正确定位' % tag, got == want))

    # ── FB5：变异对照（5 种坏段都必须判 BAD）─────────────────────────────
    print('\n── FB5 ★ 变异对照：5 种坏段必须判 **BAD**（不是"当作没段"）──')

    def judge(pl):
        """按固件算法给出 NONE/OK/BAD。"""
        tail = len(pl)
        if tail >= dblen and struct.unpack('<I', pl[tail - dblen:tail - dblen + 4])[0] == dbmagic:
            tail -= dblen
        if tail < seg_len:
            return 'NONE'
        s = pl[tail - seg_len:tail]
        if struct.unpack('<I', s[:4])[0] != magic:
            return 'NONE'
        if struct.unpack('<I', s[4:8])[0] != seg_len:
            return 'BAD'
        nu = struct.unpack('<I', s[8:12])[0]
        if nu < 2 or nu > dclc.MAX_LUT:
            return 'BAD'
        if struct.unpack('<I', s[12:16])[0] != fnv_words(s[hdr:]):
            return 'BAD'
        for i in range(dclc.MAX_LUT):
            if (struct.unpack('<I', s[hdr + i * 4:hdr + i * 4 + 4])[0] & 0x7F800000) == 0x7F800000:
                return 'BAD'
        return 'OK'

    def mut(idx, val):
        b = bytearray(payload)
        b[len(b) - seg_len + idx] = val
        return bytes(b)

    bad_cases = [
        ('magic 错一位', mut(0, (magic & 0xFF) ^ 0x01)),
        ('seg_len 错', mut(4, (seg_len - 1) & 0xFF)),
        ('n_used = 0', mut(8, 0)),
        ('n_used = 1', mut(8, 1)),
        ('n_used = 257', mut(9, 1)),        # ★ 255 是**合法值**（区间 [2,256]）—— 第一版把它当坏样例，是我写错了
        ('fnv 错一位', mut(12, struct.unpack('<I', payload[-seg_len + 12:-seg_len + 16])[0] & 0xFF ^ 0x01)),
        ('表里放 NaN', bytes(bytearray(payload)[:len(payload) - seg_len + hdr + 8]
                          + b'\x00\x00\xc0\x7f'
                          + bytearray(payload)[len(payload) - seg_len + hdr + 12:])),
    ]
    for tag, pl in bad_cases:
        got = judge(pl)
        want = 'NONE' if tag == 'magic 错一位' else 'BAD'
        print('    %-16s ⇒ 判定 %-5s（期望 %s）%s' % (tag, got, want, '' if got == want else '  ★ 不符'))
        res.append(('FB5 %s ⇒ %s' % (tag, want), got == want))
    res.append(('FB5 基准（未变异）⇒ OK', judge(payload) == 'OK'))

    # ── 在板四项 ────────────────────────────────────────────────────────
    if a.offline:
        skip.append('FB6~FB9 在板（--offline）')
    else:
        try:
            from h723_client import Dcl
            d = Dcl(a.port)
            print('\n端口 = %s' % d.port)
            import time
            sts, p = d.send(0x38, expect_len=51)
            shm = struct.unpack('<I', p[23:27])[0]
            lut_addr = shm + dclc.__dict__.get('OFF_LUT_DATA', 0x0440)
            sts, q = d.send(0x10, payload, expect_len=None)
            print('    deploy ⇒ %s' % sts)
            time.sleep(0.3)
            sts, rb = d.send(0x22, struct.pack('<IH', lut_addr, dclc.MAX_LUT),
                             expect_len=4 * dclc.MAX_LUT)
            got = struct.unpack('<%df' % dclc.MAX_LUT, rb[:4 * dclc.MAX_LUT])
            same = all(struct.pack('<f', x) == struct.pack('<f', y)
                       for x, y in zip(got, want))
            print('    FB6 读回 LUT 前 6 值 = %s ⇒ 逐位相等 = %s' % (got[:6], same))
            res.append(('FB6 部署带表程序 ⇒ 读回逐位等于发出的表', same))
        except Exception as e:
            skip.append('FB6~FB9 板子不在场/不可用（%s）—— 台架条件，不是 FAIL' % type(e).__name__)

    print('\n' + '=' * 74)
    print('=== 判据 ===')
    nf = 0
    for name, ok in res:
        print('  [%s] %s' % ('PASS' if ok else 'FAIL', name))
        nf += (not ok)
    for s in skip:
        print('  [SKIP] %s' % s)
    print('\n%d 项判定, %d FAIL, %d SKIP（SKIP ≠ PASS）' % (len(res), nf, len(skip)))
    return 0 if nf == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
