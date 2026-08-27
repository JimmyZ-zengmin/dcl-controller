#!/usr/bin/env python3
"""精确诊断: 单步执行编译块, 观察每条指令后的 PC 和寄存器变化"""
import sys, time, struct
sys.path.insert(0, '/c/Espressif/tools/python/v6.0.1/venv/Lib/site-packages')
from pyocd.core.helpers import ConnectHelper

def decode_thumb(bytes_4, addr):
    """Decode Thumb-2 instruction from 4 bytes (little-endian memory order)"""
    w = int.from_bytes(bytes_4, 'little')
    hw0 = w & 0xFFFF          # First halfword (at low address)
    hw1 = (w >> 16) & 0xFFFF  # Second halfword (at high address)

    # Check if 32-bit (halfword[15:11] is 0b11101/0b11110/0b11111)
    op1 = (hw0 >> 11) & 0x1F

    if op1 not in (0b11101, 0b11110, 0b11111):
        # 16-bit instruction
        return decode_thumb16(hw0, w), 2

    # 32-bit instruction - decode by type
    if (hw0 & 0xF800) == 0xF240:  # MOVW/MOVT (F240/F2C0)
        rd = (hw1 >> 8) & 0xF
        imm4 = (hw1 >> 12) & 0xF
        i = (hw0 >> 11) & 1
        imm3 = (hw0 >> 12) & 0x7  # bits [14:12] of hw0
        imm8 = hw0 & 0xFF
        imm16 = (imm4 << 12) | (i << 11) | (imm3 << 8) | imm8
        if (hw0 & 0x0400):  # bit 10 = 1 means MOVT
            return f"MOVT r{rd}, #0x{imm16:04x}", 4
        else:
            return f"MOVW r{rd}, #0x{imm16:04x}", 4

    if (hw0 & 0xFE00) == 0xEA00:  # B/BL (simplified check)
        return f"B/BL 32-bit (hw0={hw0:04x})", 4

    if (hw0 & 0xFFC0) == 0x4780:  # Should be 16-bit BLX
        pass  # handled above

    # VFP instructions: hw0 starts with 0xEE or 0xED
    if (hw0 & 0xEE00) == 0xEC00 or (hw0 & 0xEE00) == 0xEE00:
        # VLDR/VSTR: hw0 = 0xEDxx, bit20=1 for L=load
        if (hw0 & 0xFF00) == 0xED00:
            l = (hw0 >> 8) & 1  # bit 8 of hw0 = bit 20 of encoding
            rn = hw1 & 0xF
            vd_hi = (hw1 >> 12) & 0xF
            d = (hw0 >> 6) & 1  # bit 6 of hw0 = bit 22 of encoding
            vd = (vd_hi << 1) | d
            imm8 = hw1 & 0xFF
            if l:
                return f"VLDR s{vd}, [r{rn}, #{imm8*4}]", 4
            else:
                return f"VSTR s{vd}, [r{rn}, #{imm8*4}]", 4
        # VFMA/VADD/VSUB/VMUL
        if (hw0 & 0xEF00) == 0xEE00:
            return f"VFP-ARITH (hw0={hw0:04x})", 4

    # ADDW: F200-F2FF range
    if (hw0 & 0xF800) == 0xF200 and (hw0 & 0x0400) == 0x0000:
        rd = (hw1 >> 8) & 0xF
        rn = (hw1 >> 12) & 0xF
        i = (hw0 >> 11) & 1
        imm3 = (hw0 >> 12) & 0x7
        imm8 = hw0 & 0xFF
        imm12 = (i << 11) | (imm3 << 8) | imm8
        return f"ADDW r{rd}, r{rn}, #0x{imm12:x}", 4

    return f"??? (hw0={hw0:04x} hw1={hw1:04x})", 4

def decode_thumb16(hw, w):
    """Decode 16-bit Thumb instruction"""
    if (hw & 0xFFC0) == 0x4780:  # BLX Rm
        rm = (hw >> 3) & 0xF
        return f"BLX r{rm}"
    if (hw & 0xFFFF) == 0x4770:
        return "BX LR"
    if (hw & 0xFFFF) == 0xBF00:
        return "NOP"
    if (hw & 0xFE00) == 0xB400:  # PUSH
        return f"PUSH (hw={hw:04x})"
    if (hw & 0xFE00) == 0xBC00:  # POP
        return f"POP (hw={hw:04x})"
    if (hw & 0xF800) == 0x4800:  # LDR Rd, [PC, #imm]
        rd = (hw >> 8) & 0x7
        imm8 = hw & 0xFF
        return f"LDR r{rd}, [PC, #{imm8*4}]"
    if (hw & 0xF800) == 0x6000:  # STR/LDR
        return f"STR/LDR (hw={hw:04x})"
    if (hw & 0xFE00) == 0x1C00:  # ADD/SUB
        return f"ADD/SUB (hw={hw:04x})"
    if (hw & 0xF800) == 0x2000:  # MOV imm
        rd = (hw >> 8) & 0x7
        imm8 = hw & 0xFF
        return f"MOV r{rd}, #{imm8}"
    if (hw & 0xE000) == 0x0000:  # LSL/LSR/ASR
        return f"SHIFT (hw={hw:04x})"
    if (hw & 0xF800) == 0x3000:  # ADD/SUB imm
        return f"ADD/SUB imm (hw={hw:04x})"
    return f"16bit? (hw={hw:04x})"

def main():
    print("=" * 70)
    print("精确诊断: 单步执行编译块")
    print("=" * 70)

    with ConnectHelper.session_with_chosen_probe(
            target_override='stm32h723xx',
            connect_mode='under-reset') as session:
        target = session.target

        # 复位并 halt
        target.reset_and_halt()

        # 清除 SCB_CFSR (写 1 清)
        target.write32(0xE000ED28, 0xFFFFFFFF)
        target.write32(0xE000ED2C, 0xFFFFFFFF)

        # 设置断点于 Test5 的 BLX (0x8000c78) 和编译块入口 (0x800)
        target.set_breakpoint(0x8000c78)  # Test5 BLX to compiled block

        # 运行到 Test5
        target.resume()
        time.sleep(1.0)
        target.halt()

        pc = target.read_core_register('pc')
        cfsr = target.read32(0xE000ED28)
        dev_neg = target.read32(0x20000034)
        print(f"\nAt Test5: PC={pc:#x} CFSR={cfsr:#x} DEV_NEG_MAX={dev_neg:#x}")

        # 读取 Test5 时的寄存器
        r0 = target.read_core_register('r0')
        r4 = target.read_core_register('r4')
        print(f"  r0={r0:#x} (compiled block addr), r4={r4:#x}")

        # 移除断点, 设置断点在编译块入口
        target.remove_breakpoint(0x8000c78)
        target.set_breakpoint(0x8000801)  # 编译块入口 (Thumb)

        # 运行到编译块入口
        target.resume()
        time.sleep(0.5)
        target.halt()

        pc = target.read_core_register('pc')
        cfsr = target.read32(0xE000ED28)
        dev_neg = target.read32(0x20000034)
        print(f"\nAt compiled block: PC={pc:#x} CFSR={cfsr:#x} DEV_NEG_MAX={dev_neg:#x}")

        # 检查是否已经进入 fault
        if cfsr != 0:
            print(f"  *** FAULT before reaching block! CFSR={cfsr:#x} ***")
            stacked_pc = target.read32(0x20000070)
            print(f"  FAULT_STACKED_PC={stacked_pc:#x}")

        target.remove_breakpoint(0x8000801)

        # 检查 CPACR (FPU 启用?)
        cpacr = target.read32(0xE000ED88)
        print(f"CPACR: {cpacr:#x} (CP10={(cpacr>>20)&3}, CP11={(cpacr>>22)&3})")

        # 现在 PC 应该在编译块入口 (0x800)
        # 单步每条指令, 观察变化
        for i in range(50):
            pc = target.read_core_register('pc')

            # 读当前指令
            mem = target.read_memory_block8(pc, 4)

            # 判断 16-bit 还是 32-bit
            hw0 = mem[0] | (mem[1] << 8)
            op1 = (hw0 >> 11) & 0x1F

            if op1 in (0b11101, 0b11110, 0b11111):
                # 32-bit
                desc, size = decode_thumb(mem, pc)
            else:
                desc = decode_thumb16(hw0, hw0)
                size = 2

            # 读关键寄存器
            r0 = target.read_core_register('r0')
            r1 = target.read_core_register('r1')
            r4 = target.read_core_register('r4')
            r8 = target.read_core_register('r8')

            # 打印
            mem_str = ' '.join(f'{b:02x}' for b in mem)
            print(f"  [{i:2d}] PC={pc:#06x} [{mem_str}] {desc}")
            print(f"       r0={r0:#x} r1={r1:#x} r4={r4:#x} r8={r8:#x}")

            # 检查是否进入 fault
            cfsr = target.read32(0xE000ED28)
            if cfsr != 0:
                print(f"\n  *** FAULT! CFSR={cfsr:#x} ***")
                # 读故障地址
                bfar = target.read32(0xE000ED38)
                print(f"  BFAR={bfar:#x}")
                break

            # 检查是否返回到 main (BX LR 到 0x8000be6+)
            if pc >= 0x8000be6 and pc <= 0x8000c60:
                print(f"\n  *** 返回到 main, 编译块执行完成! ***")
                break

            # 检查是否跳到奇怪地址
            if pc > 0x10000000 or pc < 0x100:
                if pc != 0 and pc != 1:  # prim_handler 入口
                    print(f"\n  *** 跳到异常地址: {pc:#x} ***")
                    break

            # 单步
            target.step()

            # 检查 halt 状态
            if not target.is_halted():
                print(f"\n  *** CPU 未 halt, 可能进入 fault ***")
                cfsr = target.read32(0xE000ED28)
                hfsr = target.read32(0xE000ED2C)
                print(f"  CFSR={cfsr:#x} HFSR={hfsr:#x}")
                break

        # 最终状态
        print(f"\n--- 最终状态 ---")
        pc = target.read_core_register('pc')
        cfsr = target.read32(0xE000ED28)
        hfsr = target.read32(0xE000ED2C)
        dev_neg = target.read32(0x20000034)
        print(f"PC={pc:#x} CFSR={cfsr:#x} HFSR={hfsr:#x} DEV_NEG_MAX={dev_neg:#x}")

        # 检查 WIRE_MAP[0] 是否被写入
        wire0_bytes = target.read_memory_block8(0x20000300, 4)
        wire0 = struct.unpack('<f', bytes(wire0_bytes))[0]
        print(f"WIRE_MAP[0] = {wire0}")

    print("\n" + "=" * 70)
    print("诊断完成")
    print("=" * 70)

if __name__ == '__main__':
    main()
