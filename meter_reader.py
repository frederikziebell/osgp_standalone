"""
Standalone C12.18 / OSGP optical-port meter reader.

Python port of the standalone Java tool, which in turn is a close port of the protocol
logic in the working openHAB org.openhab.binding.smartmeterosgp binding
(SmartMeterOSGPHandler). Serial I/O uses pyserial instead of jSerialComm. Framing, CRC,
the ACK/NACK handshake, service requests and table parsing intentionally mirror the Java
version as closely as possible, since that's the implementation already confirmed to work
against this meter.
"""

import logging
import threading
import time
from datetime import datetime, time as dtime

import serial

from c1218 import (
    ACK,
    IDENTITY,
    NACK,
    REQUEST_ID_IDENT,
    REQUEST_ID_LOGOFF,
    REQUEST_ID_LOGON,
    REQUEST_ID_NEGOTIATE2,
    REQUEST_ID_READ,
    REQUEST_ID_READ_PARTIAL,
    REQUEST_ID_SECURITY,
    REQUEST_ID_TERMINATE,
    START,
    C1218Baudrate,
    C1218ResponseCode,
)
from crc16 import CRC16

# slf4j has a TRACE level below DEBUG; Python's logging does not, so add it.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

logger = logging.getLogger("OsgpMeterReader")

BIG_ENDIAN = "big"
LITTLE_ENDIAN = "little"


def _order_name(order):
    return "BIG_ENDIAN" if order == BIG_ENDIAN else "LITTLE_ENDIAN"


def bb2hex(data, length=None):
    if length is None:
        length = len(data)
    return "".join("%02X " % data[i] for i in range(length))


class BufferUnderflow(Exception):
    """Raised when a reply is shorter than the parser expects.

    Stands in for Java's BufferUnderflowException, but is caught and turned into a
    warning instead of killing the process.
    """


class _Buffer:
    """Minimal read-side stand-in for java.nio.ByteBuffer, with a settable byte order."""

    def __init__(self, data, order=BIG_ENDIAN):
        self.data = bytes(data)
        self.pos = 0
        self.order = order

    @property
    def limit(self):
        return len(self.data)

    def _take(self, n):
        if self.pos + n > len(self.data):
            raise BufferUnderflow(
                "wanted %d byte(s) at offset %d but the message is only %d byte(s) long"
                % (n, self.pos, len(self.data))
            )
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def get(self):
        return self._take(1)[0]

    def get_short(self):
        return int.from_bytes(self._take(2), self.order, signed=False)

    def get_int(self):
        return int.from_bytes(self._take(4), self.order, signed=True)


class OsgpMeterReader:

    def __init__(self, port_name, baud_rate, user_id, username, password,
                 refresh_interval_seconds, logoff_interval_seconds, idle_start_time,
                 idle_seconds):
        self.port_name = port_name
        self.baud_rate = baud_rate if baud_rate > 0 else 9600
        self.user_id = user_id
        self.username = username if username is not None else ""
        self.password = password if password is not None else ""
        self.refresh_interval_seconds = refresh_interval_seconds if refresh_interval_seconds > 0 else 2
        self.logoff_interval_seconds = logoff_interval_seconds if logoff_interval_seconds > 0 else 540
        self.idle_seconds = idle_seconds
        # Parsed once here (rather than on every poll) so a malformed value is reported
        # at startup instead of after the first read cycle.
        self._idle_start = (_parse_time(idle_start_time)
                            if idle_start_time and idle_start_time.strip() else None)

        self.serial_port = None

        self._crc16_calc = CRC16()

        # Overwritten as soon as Table 0's reply is parsed, which tells us the meter's
        # actual byte order for multi-byte fields in the other tables.
        self._meter_byte_order = LITTLE_ENDIAN

        # C12.18 alternates this bit on every frame sent, so the meter can spot
        # retransmissions.
        self._toggle_control = False

        self._last_logon_time = 0.0
        # Set by request_stop(); also used to make the long sleeps interruptible.
        self._stop_requested = threading.Event()
        self._session_active = False

        # Latest live readings, exposed to the dashboard web server. Guarded by a lock
        # since it's written from the poll loop thread and read from HTTP handler threads.
        self._state_lock = threading.Lock()
        self._state = {"connected": False, "last_update": None}

        if len(self.username) > 10:
            logger.warning("Username '%s' is longer than 10 characters and will be truncated",
                           self.username)
        if len(self.password) != 20:
            logger.warning("Password is %d characters long; the meter expects exactly 20",
                           len(self.password))

    def request_stop(self):
        """Requests a graceful shutdown; takes effect within one poll cycle."""
        self._stop_requested.set()

    def get_snapshot(self):
        """Returns a copy of the latest live readings, for the dashboard web server."""
        with self._state_lock:
            return dict(self._state)

    @property
    def _running(self):
        return not self._stop_requested.is_set()

    # ---------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------

    def connect_and_run(self):
        logger.info("Opening serial port: %s at %d baud", self.port_name, self.baud_rate)
        try:
            self.serial_port = serial.Serial(
                port=self.port_name,
                baudrate=self.baud_rate,
                bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE,
                # The binding never sets parity explicitly - it just uses the serial
                # transport's default, which is NONE (8N1). We match that.
                parity=serial.PARITY_NONE,
                timeout=2.0,
                write_timeout=2.0,
                rtscts=False,
                dsrdtr=False,
            )
        except (serial.SerialException, OSError) as e:
            logger.error("Failed to open serial port %s: %s", self.port_name, e)
            return

        # Matches the working openHAB binding exactly: RTS asserted, DTR not asserted.
        # Many optical probe heads take the power for their IR LED from the RTS line.
        self.serial_port.rts = True
        self.serial_port.dtr = False

        logger.info("SerialPort %s Baud %s DataBits %s StopBits %s Parity %s RTS %s DTR %s",
                    self.port_name, self.serial_port.baudrate, self.serial_port.bytesize,
                    self.serial_port.stopbits, self.serial_port.parity,
                    self.serial_port.rts, self.serial_port.dtr)

        try:
            while self._running:
                if not self._session_active:
                    if self._is_idle_period(datetime.now().time()):
                        self._wait(30.0)
                        continue
                    if not self._establish_session():
                        logger.warning("Could not establish a session with the meter, retrying in 5s...")
                        self._wait(5.0)
                        continue
                    self._last_logon_time = time.monotonic()
                    self._session_active = True

                successfully_read = self._read_and_display_live_values()

                session_too_old = (time.monotonic() - self._last_logon_time
                                   + self.refresh_interval_seconds) > self.logoff_interval_seconds
                if session_too_old:
                    self._terminate_session()
                    self._session_active = False
                elif not successfully_read:
                    self._session_active = False

                self._wait(float(self.refresh_interval_seconds))
        finally:
            if self._session_active:
                self._terminate_session()
            self._close_port()

    @staticmethod
    def _sleep(seconds):
        """Short protocol timing delay - deliberately NOT interruptible, so that a
        shutdown request cannot cut a frame exchange (including the final logoff) short."""
        time.sleep(seconds)

    def _wait(self, seconds):
        """Long between-poll sleep; returns early when a shutdown has been requested."""
        self._stop_requested.wait(seconds)

    def _is_idle_period(self, now):
        start_time = self._idle_start
        if start_time is None or self.idle_seconds <= 0:
            return False
        start_secs = start_time.hour * 3600 + start_time.minute * 60 + start_time.second
        end_secs = (start_secs + self.idle_seconds) % 86400  # wraps like LocalTime.plusSeconds
        end_time = dtime(end_secs // 3600, (end_secs % 3600) // 60, end_secs % 60)
        if start_time <= end_time:
            return start_time <= now < end_time
        return not (end_time <= now < start_time)

    # ---------------------------------------------------------------
    # Session lifecycle
    # ---------------------------------------------------------------

    def _establish_session(self):
        self._toggle_control = False

        logger.info("Sending IDENT...")
        if not self._send_request_id(REQUEST_ID_IDENT):
            logger.error("Failed to send or receive IDENT")
            self._terminate_session()
            return False

        logger.info("Sending NEGOTIATE...")
        if not self._send_negotiate_request():
            logger.error("Failed to send or receive NEGOTIATE")
            return False

        logger.info("Sending LOGON (userId=%d, username='%s')...", self.user_id, self.username)
        if not self._send_logon_request(self.user_id, self.username):
            logger.error("Failed to send or receive LOGON")
            return False

        logger.info("Sending SECURITY (password hidden)...")
        if not self._send_security_request(self.password):
            logger.error("Failed to send or receive SECURITY - check your 20-character password")
            return False

        logger.info("Reading Table 0 (general configuration)...")
        table0 = self._send_read_table(0)
        if table0 is None:
            logger.error("Failed to read Table 0")
            return False
        try:
            self._handle_table0_reply(table0)
        except BufferUnderflow as e:
            logger.error("Table 0 reply was too short to parse: %s", e)
            return False

        logger.info("Session established.")
        return True

    def _read_and_display_live_values(self):
        ok = False
        try:
            table28 = self._send_read_partial_table(28, 0, 40)
            if table28 is not None and self._handle_table28_reply(table28):
                table23 = self._send_read_partial_table(23, 0, 8)
                if table23 is not None and self._handle_table23_reply(table23):
                    ok = True
        except BufferUnderflow as e:
            logger.warning("Truncated table reply: %s", e)
        if not ok:
            logger.warning("Failed to read live values from the meter this cycle.")
        with self._state_lock:
            self._state["connected"] = ok
            if ok:
                self._state["last_update"] = datetime.now().isoformat(timespec="seconds")
        return ok

    def _terminate_session(self):
        if self._send_request_id(REQUEST_ID_LOGOFF):
            logger.debug("Logoff successful")
        if self._send_request_id(REQUEST_ID_TERMINATE):
            logger.debug("Session terminated")

    # ---------------------------------------------------------------
    # C12.18 service requests
    # ---------------------------------------------------------------

    def _send_request_id(self, request):
        if not self._send_frame(bytes([request])):
            return False
        return self._receive_frame_and_check_ack() is not None

    def _send_negotiate_request(self):
        msg = bytearray()
        msg.append(REQUEST_ID_NEGOTIATE2)
        msg += (64).to_bytes(2, BIG_ENDIAN)  # maximum packet size
        msg.append(0x02)  # maximum packets for reassembly
        msg.append(int(C1218Baudrate.Baud_9600))
        if not self._send_frame(bytes(msg)):
            return False
        return self._receive_frame_and_check_ack() is not None

    def _send_logon_request(self, user_id, username):
        safe_username = username[:10]
        msg = bytearray()
        msg.append(REQUEST_ID_LOGON)
        msg += (user_id & 0xFFFF).to_bytes(2, BIG_ENDIAN)
        for ch in safe_username:
            msg.append(ord(ch) & 0xFF)
        while len(msg) < 1 + 12:
            msg.append(ord(' '))  # space-padded, per C12.18
        if not self._send_frame(bytes(msg)):
            return False
        return self._receive_frame_and_check_ack() is not None

    def _send_security_request(self, password):
        msg = bytearray()
        msg.append(REQUEST_ID_SECURITY)
        for ch in password[:20]:
            msg.append(ord(ch) & 0xFF)
        while len(msg) < 1 + 20:
            msg.append(0)  # null-padded, NOT space-padded
        # hide_contents=True: never log the password
        if not self._send_frame(bytes(msg), hide_contents=True):
            return False
        return self._receive_frame_and_check_ack() is not None

    def _send_read_table(self, table):
        msg = bytearray()
        msg.append(REQUEST_ID_READ)
        msg += (table & 0xFFFF).to_bytes(2, BIG_ENDIAN)
        if not self._send_frame(bytes(msg)):
            return None
        return self._receive_frame_and_check_ack()

    def _send_read_partial_table(self, table, offset, bytes_to_read):
        msg = bytearray()
        msg.append(REQUEST_ID_READ_PARTIAL)
        msg += (table & 0xFFFF).to_bytes(2, BIG_ENDIAN)
        msg.append((offset >> 16) & 0xFF)
        msg += (offset & 0xFFFF).to_bytes(2, BIG_ENDIAN)
        msg += (bytes_to_read & 0xFFFF).to_bytes(2, BIG_ENDIAN)
        if not self._send_frame(bytes(msg)):
            return None
        return self._receive_frame_and_check_ack()

    # ---------------------------------------------------------------
    # Serial I/O helpers
    # ---------------------------------------------------------------

    def _available(self):
        return self.serial_port.in_waiting

    def _read_byte(self):
        """Returns the byte value, or -1 on a read timeout (matches the Java stream that
        suppresses timeout exceptions and returns -1 instead)."""
        data = self.serial_port.read(1)
        return data[0] if data else -1

    def _write(self, data):
        self.serial_port.write(data)
        self.serial_port.flush()

    # ---------------------------------------------------------------
    # C12.18 link-layer framing: START/IDENTITY/CONTROL/SEQUENCE/LENGTH/DATA/CRC,
    # with a single-byte ACK/NACK handshake in both directions.
    # ---------------------------------------------------------------

    def _send_frame(self, message, hide_contents=False):
        msg = bytearray()
        msg.append(START)
        msg.append(IDENTITY)
        msg.append(0x20 if self._toggle_control else 0x00)
        msg.append(0)  # sequence
        msg += len(message).to_bytes(2, BIG_ENDIAN)
        self._toggle_control = not self._toggle_control
        msg += message
        crc = self._crc16_calc.calculate(msg, len(msg), 0xFFFF) ^ 0xFFFF
        msg += crc.to_bytes(2, LITTLE_ENDIAN)
        send = bytes(msg)
        send_log = "<hidden>" if hide_contents else bb2hex(send)

        try:
            for _attempt in range(3):
                pending = self._available()
                if pending > 0:
                    unknown = self.serial_port.read(pending)
                    logger.log(TRACE, "Discarding unexpected pending data %s", bb2hex(unknown))
                logger.log(TRACE, "Sending %s", send_log)
                self._write(send)
                current = self._read_byte()
                if current < 0:
                    logger.warning("No reply received after sending %s", send_log)
                    return False
                if current == ACK:
                    logger.log(TRACE, "Received ACK")
                    return True
                if current == NACK:
                    logger.log(TRACE, "Received NACK after sending %s, retrying", send_log)
                    self._sleep(0.010)
                elif current == 0:
                    logger.log(TRACE, "Received 0x00, accepting as ACK")
                    return True
                else:
                    logger.warning("Received unexpected response %02X after sending %s",
                                   current, send_log)
                    self._sleep(2.0)
            logger.warning("Failed 3 times to correctly send a frame")
        except (serial.SerialException, OSError) as e:
            logger.warning("Error writing/reading serial port: %s", e)
        return False

    def _receive_frame(self):
        try:
            contents = bytearray()
            for _retries in range(10):
                read_actual = bytearray()
                saw_start = False
                for _tries in range(100):
                    current = self._read_byte()
                    if current < 0:
                        logger.warning("No data received while waiting for start of frame")
                        return None
                    if current == START:
                        read_actual.append(current)
                        saw_start = True
                        break
                    logger.log(TRACE, "Discarding unexpected byte %02X while waiting for "
                                      "start of frame", current)
                if not saw_start:
                    return None

                while True:
                    while self._available() > 0:
                        current = self._read_byte()
                        if current < 0:
                            break
                        read_actual.append(current)
                    # Wait states around reading so an interrupted transmission gets merged.
                    self._sleep(0.020)
                    if self._available() > 0:
                        continue
                    self._sleep(0.080)
                    if self._available() <= 0:
                        break

                frame = bytes(read_actual)
                logger.log(TRACE, "Received %s", bb2hex(frame))
                if frame[0] != START:
                    logger.warning("First byte of frame was not the expected start byte")
                    return None
                if len(frame) < 6:
                    logger.warning("Frame is too short to contain a header (%s)", bb2hex(frame))
                    self._write(bytes([NACK]))
                    continue
                # frame[1] is IDENTITY
                ctrl = frame[2]
                sequence = frame[3]
                length = int.from_bytes(frame[4:6], BIG_ENDIAN)
                crc_pos = 6 + length
                if crc_pos + 2 > len(frame):
                    logger.debug("Declared frame length %d is longer than the data received (%s)",
                                 length, bb2hex(frame))
                    self._write(bytes([NACK]))
                    continue
                message_crc = int.from_bytes(frame[crc_pos:crc_pos + 2], LITTLE_ENDIAN)
                calculated_crc = self._crc16_calc.calculate(frame, crc_pos, 0xFFFF) ^ 0xFFFF
                if message_crc != calculated_crc:
                    logger.warning("Incorrect CRC on received frame %s (calculated %04X)",
                                   bb2hex(frame), calculated_crc)
                    self._write(bytes([NACK]))
                    continue
                multipacket = (ctrl & 0x80) != 0
                self._write(bytes([ACK]))
                contents += frame[6:len(frame) - 2]
                if not multipacket or sequence == 0:
                    return _Buffer(contents)
                # else: more fragments of a multi-packet response are expected; loop again.
        except (serial.SerialException, OSError) as e:
            logger.warning("Error reading from serial port: %s", e)
        return None

    def _receive_frame_and_check_ack(self):
        read_actual = self._receive_frame()
        if read_actual is None:
            return None
        if read_actual.limit < 1:
            logger.warning("Meter returned an empty reply")
            return None
        code = read_actual.get()
        if code != C1218ResponseCode.Acknowledge:
            try:
                name = C1218ResponseCode(code).name
            except ValueError:
                name = code
            logger.warning("Meter returned response code %s", name)
            return None
        return read_actual

    # ---------------------------------------------------------------
    # Table parsing
    # ---------------------------------------------------------------

    def _handle_table0_reply(self, table_data):
        table_data.get_short()  # table length - not needed here
        flags1 = table_data.get()
        self._meter_byte_order = BIG_ENDIAN if (flags1 & 0x01) else LITTLE_ENDIAN
        table_data.get()  # time format / data access method / identification format
        table_data.get()  # non-integer formats
        manufacturer = "".join(chr(table_data.get()) for _ in range(4))
        logger.info("Meter manufacturer: '%s', byte order: %s", manufacturer.strip(),
                    _order_name(self._meter_byte_order))

    def _handle_table23_reply(self, table_data):
        table_data.get_short()  # table length
        table_data.order = self._meter_byte_order
        fwd_active_energy_wh = table_data.get_int()
        rev_active_energy_wh = table_data.get_int()
        logger.debug("Table 23: Fwd Active %d Wh, Rev Active %d Wh",
                     fwd_active_energy_wh, rev_active_energy_wh)
        print("  Forward Active Energy : %s Wh (%.3f kWh)"
              % (format(fwd_active_energy_wh, ","), fwd_active_energy_wh / 1000.0))
        print("  Reverse Active Energy : %s Wh (%.3f kWh)"
              % (format(rev_active_energy_wh, ","), rev_active_energy_wh / 1000.0))
        with self._state_lock:
            self._state["fwd_active_energy_wh"] = fwd_active_energy_wh
            self._state["rev_active_energy_wh"] = rev_active_energy_wh
        return True

    def _handle_table28_reply(self, table_data):
        if table_data.limit != 44:
            logger.warning("Table 28 has unexpected length %d in message %s",
                           table_data.limit, bb2hex(table_data.data))
            return False
        table_length = table_data.get_short()
        if table_length != 0x28:
            logger.warning("Table 28 declared an unexpected internal length %d in message %s",
                           table_length, bb2hex(table_data.data))
            return False
        table_data.order = self._meter_byte_order
        fwd_active_power_w = table_data.get_int()
        rev_active_power_w = table_data.get_int()
        import_reactive_var = table_data.get_int()
        export_reactive_var = table_data.get_int()
        l1_current_ma = table_data.get_int()
        l2_current_ma = table_data.get_int()
        l3_current_ma = table_data.get_int()
        l1_voltage_mv = table_data.get_int()
        l2_voltage_mv = table_data.get_int()
        l3_voltage_mv = table_data.get_int()

        logger.debug("Table 28: Fwd %d W Rev %d W Import Reactive %d VAr Export Reactive %d VAr "
                     "L1 %d mA L2 %d mA L3 %d mA L1 %d mV L2 %d mV L3 %d mV",
                     fwd_active_power_w, rev_active_power_w, import_reactive_var,
                     export_reactive_var, l1_current_ma, l2_current_ma, l3_current_ma,
                     l1_voltage_mv, l2_voltage_mv, l3_voltage_mv)

        print("=========================================")
        print("          METER LIVE READINGS           ")
        print("=========================================")
        print("  Forward Active Power   : %s W" % format(fwd_active_power_w, ","))
        print("  Reverse Active Power   : %s W" % format(rev_active_power_w, ","))
        print("  Import Reactive Power  : %s VAr" % format(import_reactive_var, ","))
        print("  Export Reactive Power  : %s VAr" % format(export_reactive_var, ","))
        print("  L1/L2/L3 Current       : %.3f / %.3f / %.3f A"
              % (l1_current_ma / 1000.0, l2_current_ma / 1000.0, l3_current_ma / 1000.0))
        print("  L1/L2/L3 Voltage       : %.3f / %.3f / %.3f V"
              % (l1_voltage_mv / 1000.0, l2_voltage_mv / 1000.0, l3_voltage_mv / 1000.0))
        with self._state_lock:
            self._state.update({
                "fwd_active_power_w": fwd_active_power_w,
                "rev_active_power_w": rev_active_power_w,
                "import_reactive_var": import_reactive_var,
                "export_reactive_var": export_reactive_var,
                "l1_current_a": l1_current_ma / 1000.0,
                "l2_current_a": l2_current_ma / 1000.0,
                "l3_current_a": l3_current_ma / 1000.0,
                "l1_voltage_v": l1_voltage_mv / 1000.0,
                "l2_voltage_v": l2_voltage_mv / 1000.0,
                "l3_voltage_v": l3_voltage_mv / 1000.0,
            })
        return True

    # ---------------------------------------------------------------
    # Utilities
    # ---------------------------------------------------------------

    def _close_port(self):
        if self.serial_port is not None and self.serial_port.is_open:
            try:
                self.serial_port.close()
                logger.info("Serial port closed.")
            except (serial.SerialException, OSError) as e:
                logger.warning("Error closing serial port: %s", e)
        self.serial_port = None


def _parse_time(value):
    """Parses HH:MM:SS (or HH:MM), like Java's LocalTime.parse."""
    value = value.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    raise ValueError("Cannot parse time '%s'; expected HH:MM:SS" % value)
