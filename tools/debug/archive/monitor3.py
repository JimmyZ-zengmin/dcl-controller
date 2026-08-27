import time
from pyocd.core.helpers import ConnectHelper

with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    core.halt()
    hb1 = core.read_memory(0x20000018, 32)
    s1  = core.read_memory(0x20000010, 32)
    ar1 = core.read_memory(0x200000F0, 32)
    pm1 = core.read_memory(0x20000008, 32)
    print(f"H1: HB={hb1} S={s1} AR={ar1} PMIN={pm1} cy={pm1/544:.2f}us")

    core.resume()
    time.sleep(1.0)

    core.halt()
    hb2 = core.read_memory(0x20000018, 32)
    s2  = core.read_memory(0x20000010, 32)
    ar2 = core.read_memory(0x200000F0, 32)
    pm2 = core.read_memory(0x20000008, 32)
    print(f"H2: HB={hb2} S={s2} AR={ar2} PMIN={pm2} cy={pm2/544:.2f}us")
    print(f"ΔHB={hb2-hb1}  ΔS={s2-s1}")
    print(f"ISR Hz = {(s2-s1)/1.0:.0f} (expected ~10000)")
