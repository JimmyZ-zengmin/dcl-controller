#!/usr/bin/env python3
"""Reset the chip and verify new firmware boots correctly."""
PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"

from pyocd.core.helpers import ConnectHelper
import time

with ConnectHelper.session_with_chosen_probe(
    target_override="stm32h723xx",
    connect_overwrite_unique_id=PROBE
) as session:
    t = session.target
    
    # Reset the chip
    print("=== Resetting chip ===")
    t.reset()  # Hardware reset
    
    # Wait a moment for boot
    time.sleep(0.5)
    
    # Halt
    t.halt()
    
    pc = t.read_core_register("pc")
    lr = t.read_core_register("lr")
    sp = t.read_core_register("sp")
    print(f"After reset+halt:")
    print(f"  PC  = 0x{pc:08X}")
    print(f"  LR  = 0x{lr:08X}")
    print(f"  SP  = 0x{sp:08X}")
    
    # Read PWR registers
    print(f"\nPWR registers after reset:")
    cr1 = t.read32(0x58024800)
    cr3 = t.read32(0x58024808)
    csr1 = t.read32(0x5802480C)
    print(f"  PWR_CR1  = 0x{cr1:08X}")
    print(f"    SVOS[15:14]  = {(cr1 >> 14) & 3}")
    print(f"    ACTVOSRDY[13] = {(cr1 >> 13) & 1}")
    print(f"    ACTVOS[12:11] = {(cr1 >> 11) & 3}")
    print(f"  PWR_CR3  = 0x{cr3:08X}")
    print(f"    LDOEN[1]     = {(cr3 >> 1) & 1}")
    print(f"    BYPASS[0]    = {cr3 & 1}")
    print(f"  PWR_CSR1 = 0x{csr1:08X}")
    
    # Now let it run for 1 second and check again
    print(f"\n=== Running for 2 seconds ===")
    t.resume()
    time.sleep(2)
    t.halt()
    
    pc = t.read_core_register("pc")
    print(f"After 2s run:")
    print(f"  PC  = 0x{pc:08X}")
    
    cr1 = t.read32(0x58024800)
    cr3 = t.read32(0x58024808)
    csr1 = t.read32(0x5802480C)
    print(f"  PWR_CR1  = 0x{cr1:08X}")
    print(f"    SVOS[15:14]  = {(cr1 >> 14) & 3}")
    print(f"    ACTVOSRDY[13] = {(cr1 >> 13) & 1}")
    print(f"    ACTVOS[12:11] = {(cr1 >> 11) & 3}")
    print(f"  PWR_CR3  = 0x{cr3:08X}")
    print(f"    LDOEN[1]     = {(cr3 >> 1) & 1}")
    print(f"  PWR_CSR1 = 0x{csr1:08X}")
    
    # Check if stuck in VOS wait loop (0x08001EEC - 0x08001EF8)
    if 0x08001EEC <= pc <= 0x08001EF8:
        print("\n  ** STILL STUCK in VOS wait loop! **")
    elif pc == 0x08002046:
        print("\n  ** STUCK in timeout while(1)! **")
    elif pc > 0x08002090:
        print(f"\n  ** PC is past SystemInit - running application code **")
    else:
        print(f"\n  ** PC is at 0x{pc:08X} (inside SystemInit) **")
