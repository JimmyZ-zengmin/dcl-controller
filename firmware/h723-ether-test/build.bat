@echo off
setlocal
set "ST=C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin"
set "GCC=%ST%\arm-none-eabi-gcc.exe"
set "SIZE=%ST%\arm-none-eabi-size.exe"
set "OBJCPY=%ST%\arm-none-eabi-objcopy.exe"

set "MCU=-mcpu=cortex-m7 -mfpu=fpv5-d16 -mfloat-abi=hard -mthumb"
set "CFLAGS=%MCU% -std=gnu11 -g3 -O2 -ffunction-sections -fdata-sections -Wall --specs=nano.specs"

if not exist build mkdir build

echo [1/3] startup...
"%GCC%" %MCU% -x assembler-with-cpp -c startup_stm32h723zgtx.s -o build\startup.o
if %ERRORLEVEL% NEQ 0 goto :fail

echo [2/3] main.c...
"%GCC%" %CFLAGS% -c main.c -o build\main.o
if %ERRORLEVEL% NEQ 0 goto :fail

echo [3/3] link...
"%GCC%" %MCU% -T STM32H723ZGTX_FLASH.ld --specs=nano.specs -Wl,--gc-sections build\startup.o build\main.o -o build\ether-test.elf
if %ERRORLEVEL% NEQ 0 goto :fail

"%SIZE%" build\ether-test.elf
echo.
echo BUILD OK: build\ether-test.elf
exit /b 0

:fail
echo BUILD FAILED
exit /b 1
