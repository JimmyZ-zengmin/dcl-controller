#!/usr/bin/env python3
"""Watch ISR entry: set HW breakpoint at 0x18, count how many times ISR runs."""
import time
from pyocd.core.helpers import ConnectHelper
from pyocd.core.target import Target
from pyocd.debug.breakpoints.provider import (HardwareBreakpointProvider, Breakpoint)

ELF = r"D:\STM\work\dcl-controller\firmware\h723-core0\bld\core0_h723.elf"

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx') as session:
    t = session.target
    programmer = FileProgrammer(session, chip_erase="sector")
    programmer.program(ELF, file_format="elf")
    print("=== Flashed ===")
    t.reset_and_halt()

    # Set HW breakpoint at ITCM 0x18 (ISR entry)
    # Hardware breakpoints work on both Flash and ITCM
    provider = HardwareBreakpointProvider(t)
    t.deleted_breakpoint_event.subscribe(lambda x: None)

    # Use the new API
    try:
        bp = provider.set_breakpoint(0x18)
    except Exception as e:
        print(f'BP set failed: {e}')
        # Try alternative
        try:
            from pyocd.debug.breakpoints.breakpoint import Breakpoint as BP
            from pyocd.debug.breakpoints.provider import BreakpointManager
            bp_obj = Breakpoint(0x18, type=Target.BREAKPOINT_HW)
            # Add via different method
            print("Trying alternative method...")
        except:
            pass

    print(f'BP set: {bp}')
    print('Resuming CPU to see if breakpoint hits (ISR entry)')

    t.resume()

    # Poll for up to 3 seconds, counting hits
    hit_count = 0
    start = time.time()
    while time.time() - start < 3.0:
        time.sleep(0.05)
        state = t.get_state()
        if state == Target.HALTED:
            hit_count += 1
            pc = t.read_core_register("pc")
            t0 = t.read32(0x200000E0)  # SHADOW
            sr = t.read32(0x40010010) & 0xFFFF
            print(f'HIT #{hit_count}: PC=0x{pc:08X} SHADOW=0x{t0:08X} UIF={(sr>>0)&1}')
            t.resume()
            time.sleep(0.01)

    print(f'\nTotal hits in 3s: {hit_count}')
    print(f'Expected: ~30 hits (100μs × 30 = 3ms, 3s / 100μs = 30000 hits)')

    t.halt()
    provider.remove_breakpoint(bp)
