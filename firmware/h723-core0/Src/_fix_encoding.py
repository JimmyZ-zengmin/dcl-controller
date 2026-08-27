#!/usr/bin/env python3
"""Fix broken UTF-8 characters in main.c by replacing \\xef\\xbf\\xbd? patterns"""

SRC = r'd:\STM\work\dcl-controller\firmware\h723-core0\Src\main.c'

with open(SRC, 'rb') as f:
    data = f.read()

B = b'\xef\xbf\xbd?'  # The broken pattern: U+FFFD + literal '?'

# Context-based replacements: (unique_ascii_context, old_fragment, new_fragment)
# Each old/new fragment is bytes. We match the old_fragment which must be unique in the file.
fixes = [
    # === File header (lines 1-11) ===
    # Line 2: "核心0 H723 [?]ISR 引擎移植[?]"
    (b'H723 ' + B + b'ISR ' + bytes([0xe5,0xbc,0x95,0xe6,0x93,0x8e]) + bytes([0xe7,0xa7,0xbb,0xe6,0xa4,0x8d]) + B,
     b'H723 \xe7\x9a\x84 ISR \xe5\xbc\x95\xe6\x93\x8e\xe7\xa7\xbb\xe6\xa4\x8d\xe7\x89\x88'),
    # "核心0 H723 的 ISR 引擎移植版"

    # Line 4: "完整移植 ESP32-S3 核心0 架构[?]STM32H723:"
    (bytes([0xe6,0x9e,0xb6,0xe6,0x9e,0x84]) + B + b'STM32H723',
     b'\xe6\x9e\xb6\xe6\x9e\x84\xe5\x88\xb0 STM32H723'),
    # "架构到 STM32H723"

    # Line 5: "六种寄存器空[?]("
    (bytes([0xe7,0xa9,0xba]) + B + b'(SENSOR',
     b'\xe7\xa9\xba\xe9\x97\xb4 (SENSOR'),
    # "空间 ("

    # Line 6: "路由表扫描引[?]("
    (bytes([0xe6,0x89,0xab,0xe6,0x8f,0x8f,0xe5,0xbc,0x95]) + B + b'(34',
     b'\xe6\x89\xab\xe6\x8f\x8f\xe5\xbc\x95\xe6\x93\x8e (34'),
    # "引擎 ("

    # Line 7: "抖动直方[?]("
    (bytes([0xe7,0x9b,0xb4,0xe6,0x96,0xb9]) + B + b'(256',
     b'\xe7\x9b\xb4\xe6\x96\xb9\xe5\x9b\xbe (256'),
    # "图 ("

    # Line 7: "180万样[?]3分钟"
    (bytes([0xe6,0xa0,0xb7]) + B + b'3',
     b'\xe6\xa0\xb7\xe3\x80\x81 3'),
    # "样、3"

    # Line 10: "7.4ns 分辨[?]"
    (bytes([0xe8,0xbe,0xa8,0xe8,0xbe,0xa8]) + B + b'\r',
     b'\xe8\xbe\xa8\xe8\xbe\xa8\xe7\x8e\x87)\r'),
    # "率)"

    # Line 16: "CANopen NMT状[?]*/"
    (b'NMT' + bytes([0xe7,0x8a,0xb6]) + B + b'*/',
     b'NMT\xe7\x8a\xb6\xe6\x80\x81 */'),
    # "状态 */"

    # Line 41: "UART 帧协[?]──"
    (bytes([0xe5,0xb8,0xa7,0xe5,0x8d,0x8f]) + B + bytes([0xe2,0x94,0x80,0xe2,0x94,0x80]),
     b'\xe5\xb8\xa7\xe5\x8d\x8f\xe8\xae\xae \xe2\x94\x80\xe2\x94\x80'),
    # "协议 ──"

    # === Section headers (═ lines) ===
    # "寄存器定义" is in the removed block, but there are similar headers
    # Line 89-91: "数据结构 (?ESP32 完全一?)"
    (bytes([0xe6,0x95,0xb0,0xe6,0x8d,0xae,0xe7,0xbb,0x93,0xe6,0x9e,0x84,0x20,0x28]) + B + b'ESP32',
     b'\xe6\x95\xb0\xe6\x8d\xae\xe7\xbb\x93\xe6\x9e\x84 (\xe4\xb8\x8e ESP32'),
    # "(与 ESP32"

    (b'ESP32 ' + bytes([0xe5,0xae,0x8c,0xe5,0x85,0xa8,0xe4,0xb8,0x80]) + B + b')',
     b'ESP32 \xe5\xae\x8c\xe5\x85\xa8\xe4\xb8\x80\xe8\x87\xb4)'),
    # "一致)"

    # Line 105-107: "时钟初始化"
    (bytes([0xe6,0x97,0xb6,0xe9,0x92,0x9f,0xe5,0x88,0x9d,0xe5,0xa7,0x8b]) + B + b'(VOS0',
     b'\xe6\x97\xb6\xe9\x92\x9f\xe5\x88\x9d\xe5\xa7\x8b\xe5\x8c\x96 (VOS0'),
    # "初始化"

    # Line 154-156: "时钟初始化" again or "原语实现"
    # Line 323-325: "CANopen" or another section
    # These are ═══ section headers with the same broken pattern at end
]

# For the many ═══ section headers that all have the same pattern:
# The pattern is: ═══...═══\xef\xbf\xbd?*/  (at the end of ═══ lines)
# The original was probably ═══...═══╗ or ═══...═══╝
# Let's just replace \xef\xbf\xbd?*/ with just */
# i.e., strip the broken char before */

count = 0
for old, new in fixes:
    pos = data.find(old)
    if pos < 0:
        print(f"NOT FOUND: {old[:40]}...")
        continue
    data = data[:pos] + new + data[pos + len(old):]
    count += 1

# Now handle remaining ═══ section header broken chars
# Pattern: ═══\xef\xbf\xbd?*\r\n  or  ═══\xef\xbf\xbd?*/\r\n
# The ═ is \xe2\x95\x90 in UTF-8
# Replace \xef\xbf\xbd? at end of ═══ lines with nothing (just remove it)
remaining = data.count(B)
print(f"After targeted fixes: {remaining} broken chars remain")

# For remaining broken chars in ═══ lines, replace \xef\xbf\xbd? with nothing
# This is safe because these are just decorative characters in box-drawing lines
import re
# Replace \xef\xbf\xbd? that appears right before */ or \r\n in ═══ lines
data = data.replace(b'\xe2\x95\x90\xef\xbf\xbd?', b'\xe2\x95\x90')
data = data.replace(b'\xef\xbf\xbd?\xe2\x95\x90', b'\xe2\x95\x90')

# For remaining non-═══ broken chars, replace \xef\xbf\xbd? with just empty string
# This removes the broken character entirely
remaining2 = data.count(B)
if remaining2 > 0:
    print(f"After ═══ fix: {remaining2} remaining, replacing with empty")
    # Replace all remaining \xef\xbf\xbd? with empty (removing the broken char)
    data = data.replace(B, b'')

final_broken = data.count(b'\xef\xbf\xbd')
print(f"Final broken chars: {final_broken}")

with open(SRC, 'wb') as f:
    f.write(data)

print(f"DONE! Applied {count} targeted fixes + bulk cleanup")
