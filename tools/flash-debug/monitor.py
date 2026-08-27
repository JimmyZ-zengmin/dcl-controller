import time, sys
from pyocd.core.helpers import ConnectHelper
print("Connecting (attach, no halt)...")
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    r = lambda a: core.read_memory(a, 32)
    last_hb = 0
    last_t = time.time()
    for i in range(50):
        hb = r(0x20000018)
        s  = r(0x20000010)
        pm = r(0x20000008)
        px = r(0x2000000C)
        ar = r(0x200000F0)
        hb_delta = hb - last_hb
        last_hb = hb
        print(f"[{i:2d}] SAMPLES={s:>10d} HB={hb:>10d} Δ={hb_delta:>6d} AR={ar} PMIN={pm} PMAX={px}")
        time.sleep(0.5)
