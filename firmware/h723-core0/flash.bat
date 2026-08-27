@echo off
echo === Flash firmware ===
py -3 -m pyocd flash -t stm32h723xx build\core0_h723.elf
echo === Reset and run ===
py -3 -m pyocd reset -t stm32h723xx
echo === DONE: engine should be running ===
