#!/usr/bin/env python3
"""
STM32 DFU flash tool using pyusb
Flashes firmware via USB using the built-in STM32 DFU bootloader
"""
import sys
import time
import usb.core
import usb.util
import struct

# USB DFU class constants
DFU_REQUEST_DETACH    = 0
DFU_REQUEST_DNLOAD    = 1
DFU_REQUEST_UPLOAD    = 2
DFU_REQUEST_GETSTATUS = 3
DFU_REQUEST_CLRSTATUS = 4
DFU_REQUEST_GETSTATE  = 5
DFU_REQUEST_ABORT     = 6

# DFU status codes
DFU_STATUS_OK = 0x00

# DFU state codes
STATE_IDLE           = 0x02
STATE_BUSY           = 0x04
STATE_DNLOAD_IDLE    = 0x05
STATE_MANIFEST       = 0x07

# STM32 DFU commands
STM32_CMD_GET_COMMANDS   = 0x00
STM32_CMD_SET_ADDRESS    = 0x21
STM32_CMD_ERASE          = 0x41
STM32_CMD_READ_UNPROTECT = 0x92

# STM32 DFU interface
DFU_INTERFACE = 0
DFU_TIMEOUT   = 5000
TRANSFER_SIZE = 2048  # STM32 DFU transfer size

def find_dfu_device():
    """Find STM32 DFU device"""
    # Load libusb backend
    import usb.backend.libusb1
    dll_path = r'C:\Users\min\AppData\Local\Programs\Python\Python313\Lib\site-packages\libusb_package\libusb-1.0.dll'
    backend = usb.backend.libusb1.get_backend(find_library=lambda x: dll_path)
    if backend is None:
        print("ERROR: Cannot load libusb backend")
        sys.exit(1)

    # STM32 DFU Vendor ID = 0x0483, Product ID = 0xDF11
    dev = usb.core.find(idVendor=0x0483, idProduct=0xDF11, backend=backend)
    if dev is None:
        # Try other common STM32 DFU PIDs
        for pid in [0x5721, 0x6740, 0x6840, 0xDF10]:
            dev = usb.core.find(idVendor=0x0483, idProduct=pid, backend=backend)
            if dev is not None:
                break
    return dev

def wait_for_state(dev, expected_state=None, timeout=DFU_TIMEOUT):
    """Wait for device to reach expected state"""
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        try:
            status = dev.ctrl_transfer(
                0xA1,  # bmRequestType: device-to-host, class, interface
                DFU_REQUEST_GETSTATUS,
                0,
                DFU_INTERFACE,
                6,
                DFU_TIMEOUT
            )
            state = status[4]
            if expected_state is None or state == expected_state:
                return status
        except usb.core.USBError:
            pass
        time.sleep(0.01)
    raise TimeoutError("DFU state timeout")

def dfu_clear_status(dev):
    """Clear DFU status"""
    dev.ctrl_transfer(0x21, DFU_REQUEST_CLRSTATUS, 0, DFU_INTERFACE, None, DFU_TIMEOUT)

def dfu_download(dev, block_num, data):
    """Send DFU_DNLOAD"""
    dev.ctrl_transfer(
        0x21,  # bmRequestType: host-to-device, class, interface
        DFU_REQUEST_DNLOAD,
        block_num,
        DFU_INTERFACE,
        data,
        DFU_TIMEOUT
    )

def mass_erase(dev):
    """Perform mass erase of all flash"""
    print("Erasing flash...")
    # Erase command: 0x41 followed by 0xFF (mass erase) and 0x00
    cmd = bytes([STM32_CMD_ERASE, 0xFF, 0x00])
    dfu_download(dev, 0, cmd)
    status = wait_for_state(dev, STATE_DNLOAD_IDLE)
    # Wait for erase to complete
    time.sleep(0.5)
    status = wait_for_state(dev, STATE_DNLOAD_IDLE)
    print("Erase complete")

def set_address(dev, address):
    """Set the flash address pointer"""
    addr_bytes = struct.pack('<I', address)  # Little-endian
    checksum = addr_bytes[0] ^ addr_bytes[1] ^ addr_bytes[2] ^ addr_bytes[3]
    cmd = bytes([STM32_CMD_SET_ADDRESS]) + addr_bytes + bytes([checksum])
    dfu_download(dev, 0, cmd)
    wait_for_state(dev, STATE_DNLOAD_IDLE)

def write_memory(dev, address, data):
    """Write data to flash at the given address"""
    # Set address
    set_address(dev, address)

    # Write data in TRANSFER_SIZE chunks
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + TRANSFER_SIZE]
        if len(chunk) < TRANSFER_SIZE:
            # Pad to TRANSFER_SIZE
            chunk = chunk + b'\xFF' * (TRANSFER_SIZE - len(chunk))

        # Block 2 = first data block after command block
        block_num = 2 + (offset // TRANSFER_SIZE)
        dfu_download(dev, block_num, chunk)
        wait_for_state(dev, STATE_DNLOAD_IDLE)
        offset += TRANSFER_SIZE

def leave_dfu(dev):
    """Exit DFU mode and run firmware"""
    print("Starting firmware...")
    # Send empty DNLOAD to exit DFU
    dfu_download(dev, 0, b'')
    try:
        wait_for_state(dev, STATE_MANIFEST)
    except:
        pass

def flash_firmware(firmware_path):
    """Flash firmware binary to STM32 via DFU"""
    # Read firmware
    with open(firmware_path, 'rb') as f:
        firmware = f.read()

    print(f"Firmware size: {len(firmware)} bytes")

    # Find DFU device
    dev = find_dfu_device()
    if dev is None:
        print("ERROR: No DFU device found!")
        print("Make sure the board is in DFU mode:")
        print("  1. Hold BOOT0 button")
        print("  2. Press and release RST")
        print("  3. Release BOOT0")
        print("  4. Connect USB Type-C cable")
        sys.exit(1)

    print(f"Found DFU device: {dev.manufacturer} {dev.product}")

    # Set configuration
    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass

    # Claim interface
    try:
        usb.util.claim_interface(dev, DFU_INTERFACE)
    except usb.core.USBError:
        pass

    try:
        # Wait for device to be ready
        status = wait_for_state(dev, STATE_DNLOAD_IDLE)
        print(f"DFU state: ready")

        # Mass erase
        mass_erase(dev)

        # Write firmware to flash starting at 0x08000000
        print(f"Writing firmware to 0x08000000...")
        write_memory(dev, 0x08000000, firmware)

        # Exit DFU and run firmware
        leave_dfu(dev)
        print("SUCCESS: Firmware flashed and running!")

    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        try:
            usb.util.release_interface(dev, DFU_INTERFACE)
        except:
            pass

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <firmware.bin>")
        sys.exit(1)

    flash_firmware(sys.argv[1])
