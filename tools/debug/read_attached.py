"""
Read DTCM without halting (so ISRs continue).
Uses pyocd Python API attach mode.
"""
import struct, time, sys
from pyocd.core.helpers import ConnectHelper

def fu(v):
    return struct.unpack('<I', v)[0]

def ff(v):
    return struct.unpack('<f', v)[0]

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
                                              connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    target = session.target
    t0 = time.time()

    # Read HEARTBEAT and SAMPLES twice without halting
    samples_1 = fu(target.read_memory(0x20000010, 4))
    heartbeat_1 = fu(target.read_memory(0x20000018, 4))
    period_min_1 = fu(target.read_memory(0x20000008, 4))
    period_max_1 = fu(target.read_memory(0x2000000C, 4))

    time.sleep(0.1)

    samples_2 = fu(target.read_memory(0x20000010, 4))
    heartbeat_2 = fu(target.read_memory(0x20000018, 4))
    period_min_2 = fu(target.read_memory(0x20000008, 4))
    period_max_2 = fu(target.read_memory(0x2000000C, 4))

    # WIRE_MAP values as floats
    w0 = ff(target.read_memory(0x20000400, 4))
    w1 = ff(target.read_memory(0x20000404, 4))
    w2 = ff(target.read_memory(0x20000408, 4))
    w3 = ff(target.read_memory(0x20000410, 4))
    w4 = ff(target.read_memory(0x20000414, 4))
    s0 = ff(target.read_memory(0x20000200, 4))

    print("=== No-halt DTCM Read ===")
    print(f"SAMPLES @ t=0      : {samples_1} = 0x{samples_1:08X}")
    print(f"SAMPLES @ t=0.1    : {samples_2} = 0x{samples_2:08X}")
    print(f"SAMPLES diff       : {samples_2 - samples_1} in 0.1s = {(samples_2 - samples_1)/0.1:.0f} Hz")
    print(f"HEARTBEAT diff     : {heartbeat_2 - heartbeat_1}")
    print(f"PERIOD_MIN (t=0)   : {period_min_1} cy = {period_min_1/544:.1f} μs")
    print(f"PERIOD_MAX (t=0)   : {period_max_1} cy = {period_max_1/544:.1f} μs")
    print(f"PERIOD_MIN (t=0.1) : {period_min_2} cy = {period_min_2/544:.1f} μs")
    print(f"PERIOD_MAX (t=0.1) : {period_max_2} cy = {period_max_2/544:.1f} μs")
    print(f"SENSOR[0]          : {s0} (exp 25.0)")
    print(f"WIRE[0]            : {w0} (exp 51.0 = 25*2+1)")
    print(f"WIRE[1]            : {w1} (exp 51.0 = clamp<0,100>)")
    print(f"WIRE[2]            : {w2} (exp 1.0  = 51>50)")
    print(f"WIRE[3]            : {w3} (exp ~5.1 = LPF(51,α=0.1))")
    print(f"WIRE[4]            : {w4} (exp 51.0 = DIRECT)")
