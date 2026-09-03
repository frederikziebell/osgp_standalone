"""A fake `serial` module plus a simulated C12.18 meter, so the protocol logic can be
exercised without hardware (and without pyserial installed)."""

import sys
import types


class SerialException(Exception):
    pass


ACK = 0x06
NACK = 0x15
START = 0xEE


def _crc_x25(data):
    """Independent bit-wise reference implementation of CRC-16/X-25."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def build_frame(payload, ctrl=0x00, sequence=0x00):
    frame = bytearray([START, 0x00, ctrl, sequence])
    frame += len(payload).to_bytes(2, "big")
    frame += payload
    frame += _crc_x25(frame).to_bytes(2, "little")
    return bytes(frame)


class FakeMeter:
    """Answers the exact request sequence the reader sends."""

    def __init__(self, byte_order="little"):
        self.byte_order = byte_order
        self.requests = []          # request-ID bytes seen
        self.frames = []            # full request payloads seen
        self.ctrl_bytes = []

    def handle(self, payload, ctrl):
        self.frames.append(payload)
        self.ctrl_bytes.append(ctrl)
        req = payload[0]
        self.requests.append(req)

        if req in (0x20, 0x61, 0x50, 0x51, 0x52, 0x21):
            return build_frame(bytes([0x00]))          # bare Acknowledge
        if req == 0x30 and payload[1:3] == b"\x00\x00":
            return build_frame(self._table0())
        if req == 0x3F:
            table = int.from_bytes(payload[1:3], "big")
            if table == 28:
                return build_frame(self._table28())
            if table == 23:
                return build_frame(self._table23())
        raise AssertionError("unexpected request %r" % payload)

    def _table0(self):
        flags1 = 0x01 if self.byte_order == "big" else 0x00
        body = bytes([flags1, 0x00, 0x00]) + b"ABCD" + b"\x00" * 4
        return bytes([0x00]) + len(body).to_bytes(2, "big") + body + b"\x00"

    def _table28(self):
        vals = [1234, -5678, 90, -12, 5000, 5100, 5200, 230000, 231000, 229500]
        body = b"".join(v.to_bytes(4, self.byte_order, signed=True) for v in vals)
        return bytes([0x00]) + (40).to_bytes(2, "big") + body + b"\x00"

    def _table23(self):
        vals = [123456789, 42]
        body = b"".join(v.to_bytes(4, self.byte_order, signed=True) for v in vals)
        return bytes([0x00]) + (8).to_bytes(2, "big") + body + b"\x00"


class Serial:
    def __init__(self, port=None, baudrate=9600, bytesize=8, stopbits=1, parity="N",
                 timeout=None, write_timeout=None, rtscts=False, dsrdtr=False, meter=None):
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.stopbits = stopbits
        self.parity = parity
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.rts = True
        self.dtr = True
        self.is_open = True
        self._rx = bytearray()          # bytes waiting to be read by the reader
        self._tx = bytearray()          # partial frame being written by the reader
        self._meter = meter if meter is not None else CURRENT_METER[0]
        self._pending = []              # frames queued, released one ACK at a time
        self._last_sent = None
        self.acks_received = 0
        self.nacks_received = 0

    def queue_frames(self, *frames):
        """Queue frames as a real meter would: the first is sent now, each later one
        only after the reader ACKs the previous."""
        self._pending.extend(frames)
        self._release_next()

    def _release_next(self):
        if self._pending:
            self._last_sent = self._pending.pop(0)
            self._rx += self._last_sent

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, size=1):
        data = bytes(self._rx[:size])
        del self._rx[:size]
        return data                      # short/empty read == timeout

    def write(self, data):
        for b in data:
            if not self._tx and b in (ACK, NACK):
                if b == ACK:
                    self.acks_received += 1
                    self._release_next()
                else:
                    self.nacks_received += 1
                    if self._last_sent is not None:
                        self._rx += self._last_sent   # retransmit on NACK
                continue
            self._tx.append(b)
            self._maybe_dispatch()
        return len(data)

    def _maybe_dispatch(self):
        if len(self._tx) < 6:
            return
        length = int.from_bytes(self._tx[4:6], "big")
        total = 6 + length + 2
        if len(self._tx) < total:
            return
        frame = bytes(self._tx[:total])
        del self._tx[:total]
        assert frame[0] == START, "bad start byte"
        assert _crc_x25(frame[:-2]) == int.from_bytes(frame[-2:], "little"), "bad CRC from reader"
        reply = self._meter.handle(frame[6:6 + length], frame[2])
        self._rx.append(ACK)          # link-layer ack for the reader's request frame
        self.queue_frames(reply)

    def flush(self):
        pass

    def close(self):
        self.is_open = False


CURRENT_METER = [None]

EIGHTBITS = 8
STOPBITS_ONE = 1
PARITY_NONE = "N"


def install(meter):
    """Registers this module as `serial` so meter_reader can import it."""
    CURRENT_METER[0] = meter
    mod = types.ModuleType("serial")
    mod.Serial = Serial
    mod.SerialException = SerialException
    mod.EIGHTBITS = EIGHTBITS
    mod.STOPBITS_ONE = STOPBITS_ONE
    mod.PARITY_NONE = PARITY_NONE
    sys.modules["serial"] = mod
    return mod
