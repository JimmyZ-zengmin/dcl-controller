@echo off
setlocal enabledelayedexpansion
title DCL Controller - UART Flash (CH340)

set BIN=build\core0_h723.bin
set PORT=COM13
set BAUDRATE=115200

if not exist "%BIN%" (
    echo ERROR: Binary not found at %BIN%
    echo Run build.bat first to compile the firmware.
    exit /b 1
)

echo ============================================
echo  DCL Controller - UART Flash via CH340
echo ============================================
echo.
echo  Binary: %BIN%
echo  Port:   %PORT%
echo.
echo  BEFORE PROCEEDING:
echo   1. Set BOOT0 jumper to HIGH (1-position)
echo   2. Press RESET button on the board
echo   3. Then press any key to start flashing
echo ============================================
echo.
pause

echo.
echo [1/4] Detecting STM32 bootloader...
py -3 -m stm32loader -p %PORT% -b %BAUDRATE% -f H7 -V
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Could not detect STM32 bootloader!
    echo Check: BOOT0=HIGH? RESET pressed? Wiring correct?
    echo   CH340 TX -- PA10 (MCU RX)
    echo   CH340 RX -- PA9  (MCU TX)
    echo   GND    -- GND
    pause
    exit /b 1
)

echo.
echo [2/4] Erasing flash...
py -3 -m stm32loader -p %PORT% -b %BAUDRATE% -f H7 -e -V
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Erase failed!
    pause
    exit /b 1
)

echo.
echo [3/4] Writing firmware...
py -3 -m stm32loader -p %PORT% -b %BAUDRATE% -f H7 --write -a 0x08000000 "%BIN%" -V
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Write failed!
    pause
    exit /b 1
)

echo.
echo [4/4] Verifying firmware...
py -3 -m stm32loader -p %PORT% -b %BAUDRATE% -f H7 -v -a 0x08000000 "%BIN%" -V
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Verify failed!
    pause
    exit /b 1
)

echo.
echo ============================================
echo  FLASH SUCCESSFUL!
echo ============================================
echo.
echo  NEXT STEPS:
echo   1. Set BOOT0 jumper back to LOW (0-position)
echo   2. Press RESET to run the firmware
echo   3. The engine will start and wait for deploy
echo   4. Use IDE CLI to deploy DCL program
echo ============================================
pause
