import struct, time
from pyocd.core.helpers import ConnectHelper

def rf(core, addr):
    v = core.read_memory(addr, 32)
    return struct.unpack('<f', struct.pack('<I', v))[0]

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
    connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    r = lambda a: core.read_memory(a, 32)
    t0 = time.time()
    s1 = r(0x20000010); h1 = r(0x20000018); p1 = r(0x20000008)
    time.sleep(0.1)
    s2 = r(0x20000010); h2 = r(0x20000018); p2 = r(0x2000000C)
    print("SAMPLES Hz:", (s2-s1)/0.1)
    print("PERIOD_MIN:", r(0x20000008), "cy =", r(0x20000008)/544, "us")
    print("PERIOD_MAX:", r(0x2000000C), "cy =", r(0x2000000C)/544, "us")
    print("SENSOR[0]:", rf(core, 0x20000200))
    print("WIRE[0] SCALE:", rf(core, 0x20000400), "(exp 51)")
    print("WIRE[1] CLAMP:", rf(core, 0x20000404), "(exp 51)")
    print("WIRE[2] CMP:", rf(core, 0x20000408), "(exp 1)")
    print("WIRE[3] LPF:", rf(core, 0x20000410), "(exp ~5.1)")
    print("WIRE[4] DIR:", rf(core, 0x20000414), "(exp 51)")
