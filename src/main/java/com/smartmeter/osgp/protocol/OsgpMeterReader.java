package com.smartmeter.osgp.protocol;

import com.fazecast.jSerialComm.SerialPort;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.time.LocalDateTime;
import java.time.LocalTime;

import static com.smartmeter.osgp.protocol.C1218Constants.*;

/**
 * Standalone C12.18 / OSGP optical-port meter reader.
 *
 * This is a close port of the protocol logic in the working openHAB
 * org.openhab.binding.smartmeterosgp binding (SmartMeterOSGPHandler), adapted to run outside of
 * openHAB using jSerialComm for serial I/O instead of openHAB's serial transport. Framing, CRC,
 * the ACK/NACK handshake, service requests and table parsing intentionally mirror the binding as
 * closely as possible, since that's the implementation already confirmed to work against this
 * meter.
 */
public class OsgpMeterReader {

    private static final Logger logger = LoggerFactory.getLogger(OsgpMeterReader.class);

    private final String portName;
    private final int baudRate;
    private final int userId;
    private final String username;
    private final String password;
    private final int refreshIntervalSeconds;
    private final int logoffIntervalSeconds;
    private final String idleStartTime;
    private final int idleSeconds;

    private SerialPort serialPort;
    private InputStream inputStream;
    private OutputStream outputStream;

    private final CRC16 crc16Calc = new CRC16(CRC16.Polynom.CRC16_CCIT);

    // Overwritten as soon as Table 0's reply is parsed, which tells us the meter's actual
    // byte order for multi-byte fields in the other tables.
    private ByteOrder meterByteOrder = ByteOrder.LITTLE_ENDIAN;

    // C12.18 alternates this bit on every frame sent, so the meter can spot retransmissions.
    private boolean toggleControl = false;

    private long lastLogonTime = 0;
    private volatile boolean running = true;
    private boolean sessionActive = false;

    public OsgpMeterReader(String portName, int baudRate, int userId, String username, String password,
            int refreshIntervalSeconds, int logoffIntervalSeconds, String idleStartTime, int idleSeconds) {
        this.portName = portName;
        this.baudRate = baudRate > 0 ? baudRate : 9600;
        this.userId = userId;
        this.username = username != null ? username : "";
        this.password = password != null ? password : "";
        this.refreshIntervalSeconds = refreshIntervalSeconds > 0 ? refreshIntervalSeconds : 2;
        this.logoffIntervalSeconds = logoffIntervalSeconds > 0 ? logoffIntervalSeconds : 540;
        this.idleStartTime = idleStartTime;
        this.idleSeconds = idleSeconds;

        if (this.username.length() > 10) {
            logger.warn("Username '{}' is longer than 10 characters and will be truncated", this.username);
        }
        if (this.password.length() != 20) {
            logger.warn("Password is {} characters long; the meter expects exactly 20", this.password.length());
        }
    }

    /** Requests a graceful shutdown; takes effect within one poll cycle. */
    public void requestStop() {
        running = false;
    }

    public void connectAndRun() {
        logger.info("Opening serial port: {} at {} baud", portName, baudRate);
        serialPort = SerialPort.getCommPort(portName);
        serialPort.setBaudRate(baudRate);
        serialPort.setNumDataBits(8);
        serialPort.setNumStopBits(1);
        // The binding never sets parity explicitly - it just uses the serial transport's
        // default, which is NONE (8N1). We match that instead of guessing EVEN.
        serialPort.setParity(SerialPort.NO_PARITY);
        serialPort.setComPortTimeouts(SerialPort.TIMEOUT_READ_SEMI_BLOCKING | SerialPort.TIMEOUT_WRITE_BLOCKING, 2000,
                2000);

        if (!serialPort.openPort()) {
            logger.error("Failed to open serial port {}", portName);
            return;
        }

        // The "suppressed timeout exceptions" stream makes a timed-out read() return -1
        // instead of throwing, which is the behaviour the ported protocol logic below expects
        // (it mirrors how openHAB's serial transport behaves on a receive timeout).
        inputStream = serialPort.getInputStreamWithSuppressedTimeoutExceptions();
        outputStream = serialPort.getOutputStream();

        // Matches the working openHAB binding exactly: RTS asserted, DTR not asserted.
        // Many optical probe heads take the power for their IR LED from the RTS line.
        serialPort.setRTS();
        serialPort.clearDTR();

        logger.info("SerialPort {} Baud {} DataBits {} StopBits {} Parity {} RTS {} DTR {}", portName,
                serialPort.getBaudRate(), serialPort.getNumDataBits(), serialPort.getNumStopBits(),
                serialPort.getParity(), serialPort.getRTS(), serialPort.getDTR());

        try {
            while (running) {
                if (!sessionActive) {
                    if (isIdlePeriod(LocalDateTime.now().toLocalTime())) {
                        sleep(30_000);
                        continue;
                    }
                    if (!establishSession()) {
                        logger.warn("Could not establish a session with the meter, retrying in 5s...");
                        sleep(5000);
                        continue;
                    }
                    lastLogonTime = System.currentTimeMillis();
                    sessionActive = true;
                }

                boolean successfullyRead = readAndDisplayLiveValues();

                boolean sessionTooOld = System.currentTimeMillis() - lastLogonTime
                        + (refreshIntervalSeconds * 1000L) > logoffIntervalSeconds * 1000L;
                if (sessionTooOld) {
                    terminateSession();
                    sessionActive = false;
                } else if (!successfullyRead) {
                    sessionActive = false;
                }

                sleep(refreshIntervalSeconds * 1000L);
            }
        } finally {
            if (sessionActive) {
                terminateSession();
            }
            closePort();
        }
    }

    private void sleep(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            running = false;
        }
    }

    private boolean isIdlePeriod(LocalTime time) {
        if (idleStartTime == null || idleStartTime.isBlank() || idleSeconds <= 0) {
            return false;
        }
        LocalTime startTime = LocalTime.parse(idleStartTime);
        LocalTime endTime = startTime.plusSeconds(idleSeconds);
        if (startTime.compareTo(endTime) <= 0) {
            return time.compareTo(startTime) >= 0 && time.compareTo(endTime) < 0;
        }
        return !(time.compareTo(endTime) >= 0 && time.compareTo(startTime) < 0);
    }

    // ---------------------------------------------------------------
    // Session lifecycle
    // ---------------------------------------------------------------

    private boolean establishSession() {
        toggleControl = false;

        logger.info("Sending IDENT...");
        if (!sendRequestID(RequestID_Ident)) {
            logger.error("Failed to send or receive IDENT");
            terminateSession();
            return false;
        }

        logger.info("Sending NEGOTIATE...");
        if (!sendNegotiateRequest()) {
            logger.error("Failed to send or receive NEGOTIATE");
            return false;
        }

        logger.info("Sending LOGON (userId={}, username='{}')...", userId, username);
        if (!sendLogonRequest((short) userId, username)) {
            logger.error("Failed to send or receive LOGON");
            return false;
        }

        logger.info("Sending SECURITY (password hidden)...");
        if (!sendSecurityRequest(password)) {
            logger.error("Failed to send or receive SECURITY - check your 20-character password");
            return false;
        }

        logger.info("Reading Table 0 (general configuration)...");
        ByteBuffer table0 = sendReadTable(0);
        if (table0 == null) {
            logger.error("Failed to read Table 0");
            return false;
        }
        handleTable0Reply(table0);

        logger.info("Session established.");
        return true;
    }

    private boolean readAndDisplayLiveValues() {
        boolean ok = false;
        ByteBuffer table28 = sendReadPartialTable(28, 0, 40);
        if (table28 != null && handleTable28Reply(table28)) {
            ByteBuffer table23 = sendReadPartialTable(23, 0, 8);
            if (table23 != null && handleTable23Reply(table23)) {
                ok = true;
            }
        }
        if (!ok) {
            logger.warn("Failed to read live values from the meter this cycle.");
        }
        return ok;
    }

    private void terminateSession() {
        if (sendRequestID(RequestID_Logoff)) {
            logger.debug("Logoff successful");
        }
        if (sendRequestID(RequestID_Terminate)) {
            logger.debug("Session terminated");
        }
    }

    // ---------------------------------------------------------------
    // C12.18 service requests
    // ---------------------------------------------------------------

    private boolean sendRequestID(byte request) {
        if (!sendFrame(new byte[] { request }, false)) {
            return false;
        }
        return receiveFrameAndCheckAck() != null;
    }

    private boolean sendNegotiateRequest() {
        ByteBuffer msg = ByteBuffer.allocate(1 + 4);
        msg.put(RequestID_Negotiate2);
        msg.putShort((short) 64); // maximum packet size
        msg.put((byte) 0x02); // maximum packets for reassembly
        msg.put((byte) (C1218Baudrate.Baud_9600.ordinal()));
        if (!sendFrame(msg.array(), false)) {
            return false;
        }
        return receiveFrameAndCheckAck() != null;
    }

    private boolean sendLogonRequest(short userId, String username) {
        String safeUsername = username.length() > 10 ? username.substring(0, 10) : username;
        ByteBuffer msg = ByteBuffer.allocate(1 + 12);
        msg.put(RequestID_Logon);
        msg.putShort(userId);
        for (int n = 0; n < safeUsername.length(); n++) {
            msg.put((byte) safeUsername.charAt(n));
        }
        while (msg.hasRemaining()) {
            msg.put((byte) ' '); // space-padded, per C12.18
        }
        if (!sendFrame(msg.array(), false)) {
            return false;
        }
        return receiveFrameAndCheckAck() != null;
    }

    private boolean sendSecurityRequest(String password) {
        ByteBuffer msg = ByteBuffer.allocate(1 + 20);
        msg.put(RequestID_Security);
        int n = 0;
        for (; n < password.length() && n < 20; n++) {
            msg.put((byte) password.charAt(n));
        }
        for (; n < 20; n++) {
            msg.put((byte) 0); // null-padded, NOT space-padded
        }
        if (!sendFrame(msg.array(), true)) { // hideContents=true: never log the password
            return false;
        }
        return receiveFrameAndCheckAck() != null;
    }

    private ByteBuffer sendReadTable(int table) {
        ByteBuffer msg = ByteBuffer.allocate(3);
        msg.put(RequestID_Read);
        msg.putShort((short) table);
        if (!sendFrame(msg.array(), false)) {
            return null;
        }
        return receiveFrameAndCheckAck();
    }

    private ByteBuffer sendReadPartialTable(int table, int offset, int bytesToRead) {
        ByteBuffer msg = ByteBuffer.allocate(8);
        msg.put(RequestID_ReadPartial);
        msg.putShort((short) table);
        msg.put((byte) (offset >> 16));
        msg.putShort((short) offset);
        msg.putShort((short) bytesToRead);
        if (!sendFrame(msg.array(), false)) {
            return null;
        }
        return receiveFrameAndCheckAck();
    }

    // ---------------------------------------------------------------
    // C12.18 link-layer framing: START/IDENTITY/CONTROL/SEQUENCE/LENGTH/DATA/CRC,
    // with a single-byte ACK/NACK handshake in both directions.
    // ---------------------------------------------------------------

    private boolean sendFrame(byte[] message, boolean hideContents) {
        ByteBuffer msg = ByteBuffer.allocate(message.length + 8);
        msg.put(START);
        msg.put(IDENTITY);
        msg.put((byte) (toggleControl ? 0x20 : 0x00));
        msg.put((byte) 0); // sequence
        msg.putShort((short) message.length);
        toggleControl = !toggleControl;
        msg.put(message);
        int crc = crc16Calc.calculate(msg.array(), msg.position(), 0xFFFF) ^ 0xFFFF;
        msg.order(ByteOrder.LITTLE_ENDIAN);
        msg.putShort((short) crc);
        byte[] send = msg.array();
        String sendLog = hideContents ? "<hidden>" : bb2hex(send);

        try {
            for (int attempt = 0; attempt < 3; attempt++) {
                if (inputStream.available() > 0) {
                    byte[] unknown = inputStream.readNBytes(inputStream.available());
                    logger.trace("Discarding unexpected pending data {}", bb2hex(unknown));
                }
                logger.trace("Sending {}", sendLog);
                outputStream.write(send);
                outputStream.flush();
                int current = inputStream.read();
                if (current < 0) {
                    logger.warn("No reply received after sending {}", sendLog);
                    return false;
                }
                if ((byte) current == ACK) {
                    logger.trace("Received ACK");
                    return true;
                }
                if ((byte) current == NACK) {
                    logger.trace("Received NACK after sending {}, retrying", sendLog);
                    sleep(10);
                } else if (current == 0) {
                    logger.trace("Received 0x00, accepting as ACK");
                    return true;
                } else {
                    logger.warn("Received unexpected response {} after sending {}", String.format("%02X", current),
                            sendLog);
                    sleep(2000);
                }
            }
            logger.warn("Failed 3 times to correctly send a frame");
        } catch (IOException e) {
            logger.warn("Error writing/reading serial port: {}", e.getMessage());
        }
        return false;
    }

    private ByteBuffer receiveFrame() {
        try {
            ByteBuffer contents = ByteBuffer.allocate(1000);
            for (int retries = 0; retries < 10; retries++) {
                ByteBuffer readActual = ByteBuffer.allocate(1000);
                boolean sawStart = false;
                for (int tries = 0; tries < 100; tries++) {
                    int current = inputStream.read();
                    if (current < 0) {
                        logger.warn("No data received while waiting for start of frame");
                        return null;
                    }
                    if ((byte) current == START) {
                        readActual.put((byte) current);
                        sawStart = true;
                        break;
                    }
                    logger.trace("Discarding unexpected byte {} while waiting for start of frame",
                            String.format("%02X", current));
                }
                if (!sawStart) {
                    return null;
                }

                do {
                    while (inputStream.available() > 0) {
                        int current = inputStream.read();
                        if (current < 0) {
                            break;
                        }
                        readActual.put((byte) current);
                    }
                    // Wait states around reading so an interrupted transmission gets merged.
                    sleep(20);
                    if (inputStream.available() > 0) {
                        continue;
                    }
                    sleep(80);
                } while (inputStream.available() > 0);

                readActual.limit(readActual.position());
                logger.trace("Received {}", bb2hex(readActual));
                readActual.rewind();
                if (readActual.get() != START) {
                    logger.warn("First byte of frame was not the expected start byte");
                    return null;
                }
                readActual.get(); // IDENTITY
                byte ctrl = readActual.get();
                byte sequence = readActual.get();
                int length = readActual.getShort();
                int crcPos = readActual.position() + length;
                if (crcPos + 2 > readActual.limit()) {
                    logger.debug("Declared frame length {} is longer than the data received ({})", length,
                            bb2hex(readActual));
                    outputStream.write(NACK);
                    continue;
                }
                readActual.order(ByteOrder.LITTLE_ENDIAN);
                int messageCrc = readActual.getChar(crcPos);
                int calculatedCrc = crc16Calc.calculate(readActual.array(), crcPos, 0xFFFF) ^ 0xFFFF;
                if (messageCrc != calculatedCrc) {
                    logger.warn("Incorrect CRC on received frame {} (calculated {})", bb2hex(readActual),
                            String.format("%04X", calculatedCrc));
                    outputStream.write(NACK);
                    continue;
                }
                boolean multipacket = (ctrl & 0x80) != 0;
                outputStream.write(ACK);
                readActual.limit(readActual.limit() - 2);
                contents.put(readActual);
                if (!multipacket || sequence == 0) {
                    contents.limit(contents.position());
                    contents.position(0);
                    ByteBuffer result = ByteBuffer.allocate(contents.limit());
                    result.put(contents);
                    result.position(0);
                    return result;
                }
                // else: more fragments of a multi-packet response are expected; loop again.
            }
        } catch (IOException e) {
            logger.warn("Error reading from serial port: {}", e.getMessage());
        }
        return null;
    }

    private ByteBuffer receiveFrameAndCheckAck() {
        ByteBuffer readActual = receiveFrame();
        if (readActual == null) {
            return null;
        }
        int code = readActual.get() & 0xFF;
        C1218ResponseCode[] codes = C1218ResponseCode.values();
        if (code >= codes.length || codes[code] != C1218ResponseCode.Acknowledge) {
            logger.warn("Meter returned response code {}", code < codes.length ? codes[code] : code);
            return null;
        }
        return readActual;
    }

    // ---------------------------------------------------------------
    // Table parsing
    // ---------------------------------------------------------------

    private void handleTable0Reply(ByteBuffer tableData) {
        tableData.getShort(); // table length - not needed here
        byte flags1 = tableData.get();
        meterByteOrder = (flags1 & 0x01) != 0 ? ByteOrder.BIG_ENDIAN : ByteOrder.LITTLE_ENDIAN;
        tableData.get(); // time format / data access method / identification format
        tableData.get(); // non-integer formats
        StringBuilder manufacturer = new StringBuilder();
        for (int n = 0; n < 4; n++) {
            manufacturer.append((char) tableData.get());
        }
        logger.info("Meter manufacturer: '{}', byte order: {}", manufacturer.toString().trim(), meterByteOrder);
    }

    private boolean handleTable23Reply(ByteBuffer tableData) {
        tableData.getShort(); // table length
        tableData.order(meterByteOrder);
        int fwdActiveEnergyWh = tableData.getInt();
        int revActiveEnergyWh = tableData.getInt();
        logger.debug("Table 23: Fwd Active {} Wh, Rev Active {} Wh", fwdActiveEnergyWh, revActiveEnergyWh);
        System.out.printf("  Forward Active Energy : %,d Wh (%.3f kWh)%n", fwdActiveEnergyWh,
                fwdActiveEnergyWh / 1000.0);
        System.out.printf("  Reverse Active Energy : %,d Wh (%.3f kWh)%n", revActiveEnergyWh,
                revActiveEnergyWh / 1000.0);
        return true;
    }

    private boolean handleTable28Reply(ByteBuffer tableData) {
        if (tableData.limit() != 44) {
            logger.warn("Table 28 has unexpected length {} in message {}", tableData.limit(), bb2hex(tableData));
            return false;
        }
        int tableLength = tableData.getShort();
        if (tableLength != 0x28) {
            logger.warn("Table 28 declared an unexpected internal length {} in message {}", tableLength,
                    bb2hex(tableData));
            return false;
        }
        tableData.order(meterByteOrder);
        int fwdActivePowerW = tableData.getInt();
        int revActivePowerW = tableData.getInt();
        int importReactiveVar = tableData.getInt();
        int exportReactiveVar = tableData.getInt();
        int l1CurrentMa = tableData.getInt();
        int l2CurrentMa = tableData.getInt();
        int l3CurrentMa = tableData.getInt();
        int l1VoltageMv = tableData.getInt();
        int l2VoltageMv = tableData.getInt();
        int l3VoltageMv = tableData.getInt();

        logger.debug(
                "Table 28: Fwd {} W Rev {} W Import Reactive {} VAr Export Reactive {} VAr "
                        + "L1 {} mA L2 {} mA L3 {} mA L1 {} mV L2 {} mV L3 {} mV",
                fwdActivePowerW, revActivePowerW, importReactiveVar, exportReactiveVar, l1CurrentMa, l2CurrentMa,
                l3CurrentMa, l1VoltageMv, l2VoltageMv, l3VoltageMv);

        System.out.println("=========================================");
        System.out.println("          METER LIVE READINGS           ");
        System.out.println("=========================================");
        System.out.printf("  Forward Active Power   : %,d W%n", fwdActivePowerW);
        System.out.printf("  Reverse Active Power   : %,d W%n", revActivePowerW);
        System.out.printf("  Import Reactive Power  : %,d VAr%n", importReactiveVar);
        System.out.printf("  Export Reactive Power  : %,d VAr%n", exportReactiveVar);
        System.out.printf("  L1/L2/L3 Current       : %.3f / %.3f / %.3f A%n", l1CurrentMa / 1000.0,
                l2CurrentMa / 1000.0, l3CurrentMa / 1000.0);
        System.out.printf("  L1/L2/L3 Voltage       : %.3f / %.3f / %.3f V%n", l1VoltageMv / 1000.0,
                l2VoltageMv / 1000.0, l3VoltageMv / 1000.0);
        return true;
    }

    // ---------------------------------------------------------------
    // Utilities
    // ---------------------------------------------------------------

    private String bb2hex(ByteBuffer bb) {
        return bb2hex(bb.array(), bb.limit());
    }

    private String bb2hex(byte[] bb) {
        return bb2hex(bb, bb.length);
    }

    private String bb2hex(byte[] bb, int length) {
        StringBuilder result = new StringBuilder();
        for (int i = 0; i < length; i++) {
            result.append(String.format("%02X ", bb[i]));
        }
        return result.toString();
    }

    private void closePort() {
        if (inputStream != null) {
            try {
                inputStream.close();
            } catch (IOException e) {
                logger.warn("Error closing input stream: {}", e.getMessage());
            }
            inputStream = null;
        }
        if (outputStream != null) {
            try {
                outputStream.close();
            } catch (IOException e) {
                logger.warn("Error closing output stream: {}", e.getMessage());
            }
            outputStream = null;
        }
        if (serialPort != null && serialPort.isOpen()) {
            serialPort.closePort();
            logger.info("Serial port closed.");
        }
    }
}