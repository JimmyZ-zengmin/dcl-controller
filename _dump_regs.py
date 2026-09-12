import sys, json, time
from pyocd.core.helpers import ConnectHelper
def dump():
    s = ConnectHelper.session_with_chosen_probe(target_override="stm32h723xx",
        options={"connect_mode":"under-reset","frequency":1000000}, blocking=False)
    s.open(); t = s.target
    t.reset_and_halt(); time.sleep(0.2); t.resume(); time.sleep(2.0); t.halt()
    out = {}
    for off in range(0x00, 0x84, 4):
        out['SDMMC_%02X' % off] = t.read32(0x52007000 + off)
    out['SDMMC_IPVR'] = t.read32(0x520073FC)
    for off in range(0x00, 0x110, 4):
        out['RCC_%03X' % off] = t.read32(0x58024400 + off)
    for off in range(0x00, 0x30, 4):
        out['PWR_%02X' % off] = t.read32(0x58024800 + off)
    for off in [0x00,0x04,0x08,0x0C,0x10,0x14,0x20,0x24,0x28]:
        out['GPIOC_%02X' % off] = t.read32(0x58020800 + off)
    for off in [0x00,0x04,0x08,0x0C,0x10,0x14,0x20,0x24,0x28]:
        out['GPIOD_%02X' % off] = t.read32(0x58020C00 + off)
    s.close()
    return out
tag = sys.argv[1]
d = dump()
json.dump(d, open('_reg_%s.json' % tag, 'w'))
print("dumped", tag, len(d), "regs")
