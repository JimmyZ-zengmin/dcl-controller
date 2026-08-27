"""
USB serial communication with the H723 controller using the DCL frame protocol.

Frame protocol:
  Command frame (PC→H723):  [0xC0] [CMD:1B] [LEN:2B LE] [PAYLOAD:LEN B] [CRC16:2B LE]
  Status  frame (H723→PC):  [0xC1] [STATUS:1B] [LEN:2B LE] [PAYLOAD:LEN B] [CRC16:2B LE]

Command codes:
  0x10 DEPLOY — download route table binary
  0x11 START  — start ISR
  0x12 STOP   — stop ISR
  0x13 RESET  — reset engine
  0x20 READ   — read WIRE (payload=[start:2B, count:2B])
  0x21 WRITE  — force WIRE (payload=[idx:2B, value:4B float])

Status codes:
  0x20 WIRE_DATA — WIRE float32 array
  0x30 HEARTBEAT — samples:4B + running:1B
  0x40 ERROR     — ASCII string

CRC-16/CCITT: polynomial 0x1021, init 0xFFFF, no reflection, no XOR out.
USB identification: VID=0x0483 (STMicroelectronics), PID=0x5740 (STM32 Virtual COM)
"""

import asyncio
import logging
import struct
from typing import Callable, Optional

import serial
import serial.tools.list_ports

logger = logging.getLogger('dcl-ide.usb')

# ── Frame markers ──────────────────────────────────────────────────────────
CMD_MARKER = 0xC0
STS_MARKER = 0xC1

# ── Command codes ──────────────────────────────────────────────────────────
CMD_DEPLOY = 0x10
CMD_START  = 0x11
CMD_STOP   = 0x12
CMD_RESET  = 0x13
CMD_READ   = 0x20
CMD_WRITE  = 0x21

# ── Status codes ───────────────────────────────────────────────────────────
STS_WIRE_DATA = 0x20
STS_HEARTBEAT = 0x30
STS_ERROR     = 0x40

# ── USB identifiers ────────────────────────────────────────────────────────
H723_VID = 0x0483
H723_PID = 0x5740

# ── Serial defaults ────────────────────────────────────────────────────────
BAUDRATE = 115200
READ_TIMEOUT = 0.1  # seconds


# ── CRC-16/CCITT ───────────────────────────────────────────────────────────

def _crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT: poly=0x1021, init=0xFFFF, no reflection, no XOR out."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


# ── USBServer ──────────────────────────────────────────────────────────────

class USBServer:
    """Manages USB serial communication with the H723 controller."""

    def __init__(self) -> None:
        self.serial_port: Optional[serial.Serial] = None
        self.reader_task: Optional[asyncio.Task] = None
        self.on_status: Optional[Callable[[int, bytes], None]] = None
        self._reader_buffer: bytearray = bytearray()

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self.serial_port is not None and self.serial_port.is_open

    # ── Port scanning ──────────────────────────────────────────────────────

    async def scan_ports(self) -> list[dict]:
        """Scan serial ports and mark the H723 device.

        Returns a list of dicts with keys: device, description, hwid, is_h723.
        """
        loop = asyncio.get_running_loop()
        ports = await loop.run_in_executor(None, serial.tools.list_ports.comports)
        result: list[dict] = []
        for p in ports:
            is_h723 = (p.vid == H723_VID and p.pid == H723_PID)
            result.append({
                'device': p.device,
                'description': p.description,
                'hwid': p.hwid,
                'is_h723': is_h723,
            })
        logger.debug("Scanned %d ports, %d H723", len(result),
                      sum(1 for r in result if r['is_h723']))
        return result

    # ── Connection management ──────────────────────────────────────────────

    async def connect(self, port_name: str) -> bool:
        """Open serial port at 115200 baud and start the background reader."""
        if self.is_connected:
            logger.warning("Already connected, disconnecting first")
            await self.disconnect()

        loop = asyncio.get_running_loop()
        try:
            self.serial_port = await loop.run_in_executor(
                None,
                lambda: serial.Serial(
                    port=port_name,
                    baudrate=BAUDRATE,
                    timeout=READ_TIMEOUT,
                    write_timeout=READ_TIMEOUT,
                ),
            )
        except serial.SerialException as exc:
            logger.error("Failed to open %s: %s", port_name, exc)
            self.serial_port = None
            return False

        self._reader_buffer.clear()
        self.reader_task = asyncio.create_task(self._reader())
        logger.info("Connected to %s @ %d baud", port_name, BAUDRATE)
        return True

    async def disconnect(self) -> None:
        """Close serial port and cancel the reader task."""
        if self.reader_task is not None:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
            self.reader_task = None

        if self.serial_port is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self.serial_port.close)
            except Exception:
                pass
            self.serial_port = None

        self._reader_buffer.clear()
        logger.info("Disconnected")

    # ── Frame I/O ──────────────────────────────────────────────────────────

    async def send_frame(self, cmd: int, payload: bytes = b'') -> None:
        """Encode and send a command frame via serial."""
        if not self.is_connected:
            logger.warning("send_frame: not connected")
            return
        frame = self.encode_frame(cmd, payload)
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.serial_port.write, frame)
            logger.debug("TX cmd=0x%02X len=%d", cmd, len(payload))
        except serial.SerialException as exc:
            logger.error("Write failed: %s", exc)
            # Attempt graceful reconnect hint
            await self._handle_serial_error()

    async def _handle_serial_error(self) -> None:
        """Handle a serial I/O error by disconnecting."""
        logger.warning("Serial error detected, disconnecting")
        await self.disconnect()

    # ── High-level commands ────────────────────────────────────────────────

    async def deploy(self, binary: bytes) -> None:
        """Send DEPLOY frame with route table binary payload."""
        logger.info("DEPLOY %d bytes", len(binary))
        await self.send_frame(CMD_DEPLOY, binary)

    async def start_engine(self) -> None:
        """Send START frame."""
        logger.info("START")
        await self.send_frame(CMD_START)

    async def stop_engine(self) -> None:
        """Send STOP frame."""
        logger.info("STOP")
        await self.send_frame(CMD_STOP)

    async def reset_engine(self) -> None:
        """Send RESET frame."""
        logger.info("RESET")
        await self.send_frame(CMD_RESET)

    async def read_wires(self, start: int, count: int) -> None:
        """Send READ frame. Payload = [start:2B LE, count:2B LE]."""
        payload = struct.pack('<HH', start, count)
        logger.debug("READ start=%d count=%d", start, count)
        await self.send_frame(CMD_READ, payload)

    async def write_wire(self, idx: int, value: float) -> None:
        """Send WRITE frame. Payload = [idx:2B LE, value:4B float LE]."""
        payload = struct.pack('<Hf', idx, value)
        logger.debug("WRITE idx=%d value=%.4f", idx, value)
        await self.send_frame(CMD_WRITE, payload)

    # ── Background reader ──────────────────────────────────────────────────

    async def _reader(self) -> None:
        """Background task: read from serial, parse status frames, call on_status."""
        loop = asyncio.get_running_loop()
        logger.debug("Reader task started")
        try:
            while True:
                try:
                    raw = await loop.run_in_executor(
                        None, self.serial_port.read, 256
                    )
                except serial.SerialException as exc:
                    logger.error("Read failed: %s", exc)
                    await self._handle_serial_error()
                    return

                if not raw:
                    continue

                self._reader_buffer.extend(raw)
                self._process_buffer()

        except asyncio.CancelledError:
            logger.debug("Reader task cancelled")
        except Exception as exc:
            logger.exception("Reader task crashed: %s", exc)

    def _process_buffer(self) -> None:
        """Try to extract complete status frames from the reader buffer."""
        while True:
            # Find next status marker
            try:
                idx = self._reader_buffer.index(STS_MARKER)
            except ValueError:
                break

            if idx > 0:
                # Discard garbage before the marker
                del self._reader_buffer[:idx]

            # Need at least: marker(1) + status(1) + len(2) = 4 bytes header
            if len(self._reader_buffer) < 4:
                break

            payload_len = struct.unpack_from('<H', self._reader_buffer, 2)[0]
            frame_len = 1 + 1 + 2 + payload_len + 2  # marker + status + len + payload + crc

            if len(self._reader_buffer) < frame_len:
                break  # incomplete frame, wait for more data

            frame_data = bytes(self._reader_buffer[:frame_len])
            del self._reader_buffer[:frame_len]

            result = self.decode_frame(frame_data)
            if result is not None:
                status_code, payload = result
                logger.debug("RX status=0x%02X len=%d", status_code, len(payload))
                if self.on_status is not None:
                    try:
                        # Schedule the async callback
                        import asyncio
                        asyncio.get_event_loop().create_task(
                            self.on_status(status_code, payload)
                        )
                    except Exception:
                        logger.exception("on_status callback error")
            else:
                logger.warning("CRC mismatch, dropping frame (%d bytes)", frame_len)

    # ── Static frame encoding / decoding ───────────────────────────────────

    @staticmethod
    def encode_frame(cmd: int, payload: bytes = b'') -> bytes:
        """Encode a command frame.

        Format: [0xC0] [CMD:1B] [LEN:2B LE] [PAYLOAD:LEN B] [CRC16:2B LE]
        CRC covers CMD + LEN + PAYLOAD.
        """
        length = len(payload)
        header = struct.pack('<BBH', CMD_MARKER, cmd, length)
        crc_data = struct.pack('<BH', cmd, length) + payload
        crc = _crc16_ccitt(crc_data)
        return header + payload + struct.pack('<H', crc)

    @staticmethod
    def decode_frame(data: bytes) -> Optional[tuple[int, bytes]]:
        """Decode a status frame.

        Returns (status_code, payload) on success, or None on CRC failure.
        Expects: [0xC1] [STATUS:1B] [LEN:2B LE] [PAYLOAD:LEN B] [CRC16:2B LE]
        """
        if len(data) < 6:
            return None

        marker = data[0]
        if marker != STS_MARKER:
            logger.warning("decode_frame: unexpected marker 0x%02X", marker)
            return None

        status_code = data[1]
        payload_len = struct.unpack_from('<H', data, 2)[0]
        expected_len = 1 + 1 + 2 + payload_len + 2

        if len(data) < expected_len:
            return None

        payload = data[4:4 + payload_len]
        received_crc = struct.unpack_from('<H', data, 4 + payload_len)[0]

        crc_data = struct.pack('<BH', status_code, payload_len) + payload
        computed_crc = _crc16_ccitt(crc_data)

        if received_crc != computed_crc:
            return None

        return (status_code, payload)
