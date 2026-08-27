#!/usr/bin/env python3
"""Build h723-core0 firmware and flash it."""
import subprocess, os, sys

TOOLCHAIN = r"C:\ST\STM32CubeIDE_1.5.1\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.7-2018-q2-update.win32_1.5.0.202011040924\tools\bin"
GCC = os.path.join(TOOLCHAIN, "arm-none-eabi-gcc.exe")
OBJCOPY = os.path.join(TOOLCHAIN, "arm-none-eabi-objcopy.exe")
SIZE = os.path.join(TOOLCHAIN, "arm-none-eabi-size.exe")

PROJ = r"D:\STM\work\dcl-controller\firmware\h723-core0"
BLD = os.path.join(PROJ, "bld")

CFLAGS = [
    "-mcpu=cortex-m7", "-mthumb", "-mfpu=fpv5-d16", "-mfloat-abi=hard",
    "-DSTM32H723xx", "-DUSE_FULL_ASSERT",
    "-I" + os.path.join(PROJ, "Inc"),
    "-I" + os.path.join(PROJ, "Inc", "clock"),
    "-I" + os.path.join(PROJ, "Inc", "adc"),
    "-I" + os.path.join(PROJ, "Inc", "canopen"),
    "-I" + os.path.join(PROJ, "Inc", "dcl_engine"),
    "-I" + os.path.join(PROJ, "Inc", "dma"),
    "-I" + os.path.join(PROJ, "Inc", "gpio"),
    "-I" + os.path.join(PROJ, "Inc", "nvic"),
    "-I" + os.path.join(PROJ, "Inc", "tim1"),
    "-I" + os.path.join(PROJ, "Inc", "uart"),
    "-Os", "-g3", "-Wall", "-ffunction-sections", "-fdata-sections",
    "-std=gnu11",
]

LIBPATH = os.path.join(TOOLCHAIN, "..", "lib", "gcc", "arm-none-eabi", "7.3.1")
LIBPATH2 = os.path.join(TOOLCHAIN, "..", "arm-none-eabi", "lib", "thumb", "v7e-m", "fpv5", "hard")

LDFLAGS = [
    "-mcpu=cortex-m7", "-mthumb", "-mfpu=fpv5-d16", "-mfloat-abi=hard",
    "-specs=nano.specs", "-specs=nosys.specs",
    "-T" + os.path.join(PROJ, "STM32H723ZGTX_FLASH.ld"),
    "-L" + LIBPATH, "-L" + LIBPATH2,
    "-Wl,--gc-sections", "-Wl,-Map=" + os.path.join(BLD, "core0_h723.map"),
    "-lc", "-lm", "-lstdc++",
]

# Source files
SRCS = [
    "Src/main.c",
    "Src/clock/clock_init.c",
    "Src/gpio/gpioe_output.c",
    "Src/dma/dma2.c",
    "Src/adc/adc1.c",
    "Src/tim1/tim1_pwm.c",
    "Src/dcl_engine/engine.c",
    "Src/canopen/canopen.c",
    "Src/nvic/nvic.c",
    "Src/uart/uart.c",
    "Src/syscalls.c",
    "Src/sysmem.c",
    "Startup/startup_stm32h723zgtx.s",
]

def build():
    objs = []
    for src in SRCS:
        src_path = os.path.join(PROJ, src)
        obj_path = os.path.join(BLD, os.path.splitext(src)[0] + ".o")
        os.makedirs(os.path.dirname(obj_path), exist_ok=True)
        
        cmd = [GCC, "-c"] + CFLAGS + ["-o", obj_path, src_path]
        print(f"  CC {src}")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERROR: {r.stderr}")
            return False
        objs.append(obj_path)
    
    elf = os.path.join(BLD, "core0_h723.elf")
    cmd = [GCC] + objs + LDFLAGS + ["-o", elf]
    print(f"  LD core0_h723.elf")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"LINK ERROR: {r.stderr}")
        return False
    
    # Size
    r = subprocess.run([SIZE, elf], capture_output=True, text=True)
    print(r.stdout)
    return True

def flash():
    """Flash using pyocd."""
    elf = os.path.join(BLD, "core0_h723.elf")
    PROBE = "00000805059ed5520a4400013dd0702a5a5a5a59796990e"
    
    from pyocd.core.helpers import ConnectHelper
    from pyocd.flash.file_programmer import FileProgrammer
    import time
    
    print(f"\n=== 烧录 {elf} ===")
    with ConnectHelper.session_with_chosen_probe(
        target_override="stm32h723xx",
        connect_overwrite_unique_id=PROBE
    ) as session:
        t = session.target
        t.halt()
        print(f"  PC before flash = 0x{t.read_core_register('pc'):08X}")
        
        FileProgrammer(session).program(elf, file_format="elf")
        print("  Flash 完成")
        
        t.reset()
        time.sleep(0.5)
        t.halt()
        
        pc = t.read_core_register("pc")
        cr3 = t.read32(0x5802480C)  # PWR VOS register
        print(f"\n  PC after reset = 0x{pc:08X}")
        print(f"  PWR_VOS (0x5802480C) = 0x{cr3:08X}")
        print(f"    VOS[5:4]   = {(cr3 >> 4) & 3}")
        print(f"    VOSRDY[6]  = {(cr3 >> 6) & 1}")
        
        if 0x08001E00 <= pc <= 0x08002100:
            print(f"  ** PC 在 SystemInit 范围内 - 可能仍在 VOS 等待 **")
        elif pc > 0x08002100:
            print(f"  ** PC 已走出 SystemInit - 启动成功! **")
        else:
            print(f"  ** PC = 0x{pc:08X} **")

if __name__ == "__main__":
    print("=== 编译 h723-core0 ===")
    if build():
        print("\n编译成功!")
        flash()
    else:
        print("\n编译失败!")
        sys.exit(1)
