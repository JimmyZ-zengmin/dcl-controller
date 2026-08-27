import time
from pyocd.core.helpers import ConnectHelper
with ConnectHelper.session_with_chosen_probe(target_override='stm32h723xx',
  connect_overwrite_unique_id='000000805059ed5520a4400013dd0702a5a5a5a59796990e') as session:
    core = session.target.selected_core_or_raise
    r = lambda a: core.read_memory(a, 32)
    for i in range(20):
        print(f"[{i:2d}] ACTIVE=0x{r(0x200000F0):08X} HB=0x{r(0x20000018):08X} SAMPLES=0x{r(0x20000010):08X} PERIOD_MIN={r(0x20000008)} cy={r(0x20000008)/544:.2f} us")
        time.sleep(0.1)
