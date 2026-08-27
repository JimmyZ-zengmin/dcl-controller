import time
from pyocd.core.helpers import ConnectHelper
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    r32 = lambda a: core.read_memory(a, 32)
    for i in range(5):
        arm = r32(0x200000F0)
        hb = r32(0x20000018)
        s = r32(0x20000010)
        pm = r32(0x20000008)
        print(f"[{i}] ACTIVE=0x{arm:08X} SAMPLES={s} HB={hb} PMIN={pm} cy={pm/544:.1f}us")
        time.sleep(0.05)
