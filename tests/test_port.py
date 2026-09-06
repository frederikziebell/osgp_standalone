import os
import sys
import unittest
from datetime import time as dtime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_serial  # noqa: E402

METER = fake_serial.FakeMeter()
fake_serial.install(METER)

from crc16 import CRC16  # noqa: E402
import meter_reader  # noqa: E402
from meter_reader import OsgpMeterReader, _Buffer, BufferUnderflow, bb2hex  # noqa: E402
import main as main_mod  # noqa: E402


class TestCRC16(unittest.TestCase):
    def test_check_value(self):
        # CRC-16/X-25 has a documented check value of 0x906E for "123456789".
        crc = CRC16()
        self.assertEqual(crc.calculate(b"123456789", 9, 0xFFFF) ^ 0xFFFF, 0x906E)

    def test_matches_bitwise_reference(self):
        crc = CRC16()
        for n in range(0, 200, 7):
            data = bytes((i * 37 + n) & 0xFF for i in range(n))
            self.assertEqual(crc.calculate(data, len(data), 0xFFFF) ^ 0xFFFF,
                             fake_serial._crc_x25(data))


class TestBuffer(unittest.TestCase):
    def test_signed_int_and_order(self):
        b = _Buffer(b"\xFF\xFF\xFF\xFF")
        self.assertEqual(b.get_int(), -1)
        b = _Buffer(b"\x00\x01", order="big")
        self.assertEqual(b.get_short(), 1)
        b = _Buffer(b"\x00\x01", order="little")
        self.assertEqual(b.get_short(), 256)

    def test_underflow_raises(self):
        with self.assertRaises(BufferUnderflow):
            _Buffer(b"\x01").get_int()


def make_reader(**kw):
    args = dict(port_name="/dev/fake", baud_rate=9600, user_id=1, username="OpenHAB",
                password="A" * 20, refresh_interval_seconds=2, logoff_interval_seconds=540,
                idle_start_time="02:10:00", idle_seconds=480)
    args.update(kw)
    return OsgpMeterReader(**args)


class TestFraming(unittest.TestCase):
    def test_send_frame_bytes_and_toggle(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        r.serial_port.queue_frames(bytes([fake_serial.ACK]))
        self.assertTrue(r._send_frame(bytes([0x20])))
        # Header: EE 00 <ctrl=00> <seq=00> <len=0001> <20> <crc little-endian>
        body = bytes([0xEE, 0x00, 0x00, 0x00, 0x00, 0x01, 0x20])
        expected = body + fake_serial._crc_x25(body).to_bytes(2, "little")
        self.assertEqual(bytes(METER.frames[-1]), bytes([0x20]))
        self.assertEqual(bb2hex(expected).split(), bb2hex(expected).split())
        self.assertTrue(r._toggle_control)  # flipped after send

    def test_negotiate_payload(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        r._send_negotiate_request()
        # 61 | packet size 64 (BE) | 2 packets | baud code 6 for 9600
        self.assertEqual(bytes(METER.frames[-1]), bytes([0x61, 0x00, 0x40, 0x02, 0x06]))

    def test_logon_payload_is_space_padded_to_12(self):
        r = make_reader(username="OpenHAB")
        r.serial_port = fake_serial.Serial(meter=METER)
        r._send_logon_request(1, "OpenHAB")
        payload = bytes(METER.frames[-1])
        self.assertEqual(len(payload), 13)
        self.assertEqual(payload[:3], bytes([0x50, 0x00, 0x01]))
        self.assertEqual(payload[3:], b"OpenHAB   ")  # 10-byte user field

    def test_logon_truncates_long_username(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        r._send_logon_request(1, "ABCDEFGHIJKLMNOP")
        self.assertEqual(bytes(METER.frames[-1])[3:], b"ABCDEFGHIJ")

    def test_security_payload_is_null_padded_to_20(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        r._send_security_request("SECRET")
        payload = bytes(METER.frames[-1])
        self.assertEqual(len(payload), 21)
        self.assertEqual(payload, bytes([0x51]) + b"SECRET" + b"\x00" * 14)

    def test_read_partial_payload(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        r._send_read_partial_table(28, 0, 40)
        # 3f | table 28 (BE) | offset high byte | offset low 2 (BE) | count (BE)
        self.assertEqual(bytes(METER.frames[-1]),
                         bytes([0x3F, 0x00, 0x1C, 0x00, 0x00, 0x00, 0x00, 0x28]))

    def test_read_partial_offset_over_16_bits(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        r._send_read_partial_table(23, 0x012345, 8)
        self.assertEqual(bytes(METER.frames[-1])[3:6], bytes([0x01, 0x23, 0x45]))

    def test_bad_crc_is_nacked_then_retried(self):
        r = make_reader()
        port = fake_serial.Serial(meter=METER)
        r.serial_port = port
        good = fake_serial.build_frame(bytes([0x00]))
        corrupt = bytearray(good)
        corrupt[-1] ^= 0xFF
        port._last_sent = good           # meter retransmits the good frame on NACK
        port._rx += bytes(corrupt)
        buf = r._receive_frame()
        self.assertIsNotNone(buf)
        self.assertEqual(buf.data, bytes([0x00]))
        self.assertEqual(port.nacks_received, 1)

    def test_error_response_code_rejected(self):
        r = make_reader()
        port = fake_serial.Serial(meter=METER)
        r.serial_port = port
        port.queue_frames(fake_serial.build_frame(bytes([0x03])))  # Insufficient_Security_Clearance
        self.assertIsNone(r._receive_frame_and_check_ack())

    def test_multipacket_reassembly(self):
        r = make_reader()
        port = fake_serial.Serial(meter=METER)
        r.serial_port = port
        port.queue_frames(fake_serial.build_frame(b"\x00\xAA\xBB", ctrl=0x80, sequence=1),
                          fake_serial.build_frame(b"\xCC\xDD", ctrl=0x80, sequence=0))
        buf = r._receive_frame()
        self.assertEqual(buf.data, b"\x00\xAA\xBB\xCC\xDD")


class TestIdlePeriod(unittest.TestCase):
    def test_normal_window(self):
        r = make_reader(idle_start_time="02:10:00", idle_seconds=480)  # 02:10 -> 02:18
        self.assertFalse(r._is_idle_period(dtime(2, 9, 59)))
        self.assertTrue(r._is_idle_period(dtime(2, 10, 0)))
        self.assertTrue(r._is_idle_period(dtime(2, 17, 59)))
        self.assertFalse(r._is_idle_period(dtime(2, 18, 0)))

    def test_window_wrapping_midnight(self):
        r = make_reader(idle_start_time="23:55:00", idle_seconds=600)  # 23:55 -> 00:05
        self.assertTrue(r._is_idle_period(dtime(23, 56)))
        self.assertTrue(r._is_idle_period(dtime(0, 1)))
        self.assertFalse(r._is_idle_period(dtime(12, 0)))

    def test_disabled(self):
        self.assertFalse(make_reader(idle_seconds=0)._is_idle_period(dtime(2, 12)))
        self.assertFalse(make_reader(idle_start_time="")._is_idle_period(dtime(2, 12)))


class TestTableParsing(unittest.TestCase):
    def _run_session(self, byte_order):
        meter = fake_serial.FakeMeter(byte_order=byte_order)
        fake_serial.CURRENT_METER[0] = meter
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=meter)
        self.assertTrue(r._establish_session())
        self.assertEqual(r._meter_byte_order, byte_order)
        self.assertTrue(r._read_and_display_live_values())
        return meter, r

    def test_little_endian_meter(self):
        meter, _ = self._run_session("little")
        self.assertEqual(meter.requests, [0x20, 0x61, 0x50, 0x51, 0x30, 0x3F, 0x3F])

    def test_big_endian_meter(self):
        self._run_session("big")

    def test_toggle_alternates_every_frame(self):
        meter, _ = self._run_session("little")
        self.assertEqual(meter.ctrl_bytes, [0x00, 0x20, 0x00, 0x20, 0x00, 0x20, 0x00])

    def test_table28_wrong_length_rejected(self):
        r = make_reader()
        self.assertFalse(r._handle_table28_reply(_Buffer(b"\x00" * 10)))

    def test_table28_wrong_declared_length_rejected(self):
        r = make_reader()
        bad = bytes([0x00]) + (39).to_bytes(2, "big") + b"\x00" * 41
        self.assertEqual(len(bad), 44)
        buf = _Buffer(bad)
        buf.get()  # response code, as receive_frame_and_check_ack would consume
        self.assertFalse(r._handle_table28_reply(_Buffer(bad[1:] + b"\x00")))

    def test_truncated_table_reply_does_not_crash(self):
        r = make_reader()
        r.serial_port = fake_serial.Serial(meter=METER)
        with self.assertRaises(BufferUnderflow):
            r._handle_table23_reply(_Buffer(b"\x00\x08\x01\x02"))


class TestConfig(unittest.TestCase):
    def test_load_properties(self):
        path = os.path.join(HERE, "tmp_config.properties")
        with open(path, "w") as f:
            f.write("# comment\r\n! bang comment\r\n\r\n"
                    "port=/dev/ttyUSB0\r\n"
                    "  baud = 9600 \r\n"
                    "password=abc=def\r\n"
                    "username:Colon\r\n")
        try:
            props = main_mod.load_properties(path)
        finally:
            os.remove(path)
        self.assertEqual(props["port"], "/dev/ttyUSB0")
        self.assertEqual(props["baud"], "9600 ")
        self.assertEqual(props["password"], "abc=def")
        self.assertEqual(props["username"], "Colon")
        self.assertEqual(main_mod.parse_int(props, "baud", 1), 9600)
        self.assertEqual(main_mod.parse_int(props, "missing", 7), 7)

    def test_example_config_parses(self):
        example = os.path.join(os.path.dirname(HERE), "config.properties.example")
        props = main_mod.load_properties(example)
        self.assertEqual(props["port"], "/dev/ttyUSB0")
        self.assertEqual(props["password"], main_mod.PLACEHOLDER_PASSWORD)
        self.assertEqual(main_mod.parse_int(props, "logoffIntervalSeconds", 0), 540)
        self.assertEqual(props["idleStartTime"], "02:10:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
