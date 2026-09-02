package com.smartmeter.osgp.protocol;

import com.fazecast.jSerialComm.SerialPort;
import com.fazecast.jSerialComm.SerialPortTimeoutException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

public class OsgpMeterReader {
    private static final Logger logger = LoggerFactory.getLogger(OsgpMeterReader.class);

    private static final byte ACK = 0x06;
    private static final byte NAK = 0x15;
    private static final byte STP = (byte) 0xEE;

    private final String portName;
    private final int baudRate;
    private final String hexKey;

    private SerialPort serialPort;
    private InputStream inputStream;
    private OutputStream outputStream;

    public OsgpMeterReader(String portName, int baudRate, String hexKey) {
        this.portName = portName;
        this.baudRate = baudRate;
        this.hexKey = hexKey;
    }

    public void start() {
        logger.info("Opening serial port: {} at {} baud (8E1)...", portName, baudRate);
        serialPort = SerialPort.getCommPort(portName);

        serialPort.setComPortParameters(baudRate, 8, SerialPort.ONE_STOP_BIT, SerialPort.EVEN_PARITY);
        serialPort.setComPortTimeouts(SerialPort.TIMEOUT_READ_BLOCKING, 2000, 0);

        if (!serialPort.openPort()) {
            logger.error("Failed to open port: {}", portName);
            return;
        }

        inputStream = serialPort.getInputStream();
        outputStream = serialPort.getOutputStream();

        try {
            Thread.sleep(400);
            flushInputBuffer();

            // Attempt 1: C12.18 LOGON Packet (Service 0x50) [Full ANSI User ID Header]
            logger.info("Attempt 1: Sending ANSI C12.18 LOGON (Service 0x50 + User ID + Key)...");
            byte[] binaryKey = hexStringToByteArray(hexKey);
            byte[] userName = "ADMIN     ".getBytes(StandardCharsets.US_ASCII); // 10 bytes padded ASCII
            
            // Payload: [0x50] + [0x00, 0x01 (User ID)] + [10 Bytes User Name] + [Binary Key]
            byte[] logonPayload = new byte[1 + 2 + userName.length + binaryKey.length];
            logonPayload[0] = 0x50; // LOGON Command
            logonPayload[1] = 0x00; // User ID MSB
            logonPayload[2] = 0x01; // User ID LSB
            System.arraycopy(userName, 0, logonPayload, 3, userName.length);
            System.arraycopy(binaryKey, 0, logonPayload, 3 + userName.length, binaryKey.length);

            if (sendC1218Frame(logonPayload, (byte) 0x00, false, false)) return;

            Thread.sleep(300);
            flushInputBuffer();

            // Attempt 2: C12.18 Negotiate Service (0x0F) [Max Packet Size 64, Baud Rate 9600]
            logger.info("Attempt 2: Sending C12.18 Negotiate Service (0x0F)...");
            byte[] negotiatePayload = new byte[]{ 0x0F, 0x00, 0x40, 0x01, 0x00 }; // 64-byte frame, 1 packet
            if (sendC1218Frame(negotiatePayload, (byte) 0x00, false, false)) return;

            Thread.sleep(300);
            flushInputBuffer();

            // Attempt 3: IDENT (0x20) without CRC Inversion XOR (0x0000 XOR end)
            logger.info("Attempt 3: Sending IDENT Request (0x20) [Standard CRC-16 No Final XOR]...");
            if (sendC1218Frame(new byte[]{ 0x20 }, (byte) 0x00, false, true)) return;

        } catch (Exception e) {
            logger.error("Error during communication execution", e);
        } finally {
            closePort();
        }
    }

    private boolean sendC1218Frame(byte[] payload, byte controlByte, boolean swapCrcBytes, boolean noXorCrc) throws Exception {
        byte[] header = new byte[]{
            STP,
            controlByte,
            (byte) ((payload.length >> 8) & 0xFF),
            (byte) (payload.length & 0xFF)
        };

        byte[] frameWithoutCrc = new byte[header.length + payload.length];
        System.arraycopy(header, 0, frameWithoutCrc, 0, header.length);
        System.arraycopy(payload, 0, frameWithoutCrc, header.length, payload.length);

        int crc = computeC1218CRC(frameWithoutCrc, noXorCrc);
        byte[] fullFrame = new byte[frameWithoutCrc.length + 2];
        System.arraycopy(frameWithoutCrc, 0, fullFrame, 0, frameWithoutCrc.length);

        if (swapCrcBytes) {
            fullFrame[fullFrame.length - 2] = (byte) ((crc >> 8) & 0xFF);
            fullFrame[fullFrame.length - 1] = (byte) (crc & 0xFF);
        } else {
            fullFrame[fullFrame.length - 2] = (byte) (crc & 0xFF);
            fullFrame[fullFrame.length - 1] = (byte) ((crc >> 8) & 0xFF);
        }

        logger.info("TX Frame: {}", bytesToHex(fullFrame, fullFrame.length));
        outputStream.write(fullFrame);
        outputStream.flush();

        byte[] buffer = new byte[128];
        int bytesRead = readWithCatch(buffer);

        if (bytesRead > 0) {
            byte[] response = new byte[bytesRead];
            System.arraycopy(buffer, 0, response, 0, bytesRead);
            logger.info("RX Response ({} bytes): {}", bytesRead, bytesToHex(response, bytesRead));

            if (response[0] == ACK) {
                logger.info("Meter returned ACK (0x06)! Session Initialized.");
                return true;
            } else if (response[0] == STP) {
                logger.info("Meter returned C12.18 response frame (0xEE)!");
                return true;
            } else if (response[0] == NAK) {
                logger.warn("Meter returned NAK (0x15).");
            }
        } else {
            logger.warn("No response received within timeout.");
        }

        return false;
    }

    private int readWithCatch(byte[] buffer) throws Exception {
        try {
            return inputStream.read(buffer);
        } catch (SerialPortTimeoutException e) {
            return 0;
        }
    }

    private void flushInputBuffer() throws Exception {
        while (inputStream.available() > 0) {
            inputStream.read();
        }
    }

    private int computeC1218CRC(byte[] data, boolean noXor) {
        int crc = 0x0000;
        for (byte b : data) {
            crc ^= (b & 0xFF);
            for (int i = 0; i < 8; i++) {
                if ((crc & 0x0001) != 0) {
                    crc = (crc >> 1) ^ 0x8408;
                } else {
                    crc >>= 1;
                }
            }
        }
        return noXor ? crc : (crc ^ 0xFFFF);
    }

    private byte[] hexStringToByteArray(String s) {
        int len = s.length();
        byte[] data = new byte[len / 2];
        for (int i = 0; i < len; i += 2) {
            data[i / 2] = (byte) ((Character.digit(s.charAt(i), 16) << 4)
                                 + Character.digit(s.charAt(i+1), 16));
        }
        return data;
    }

    private void closePort() {
        try {
            if (inputStream != null) inputStream.close();
            if (outputStream != null) outputStream.close();
        } catch (Exception ignored) {}
        if (serialPort != null && serialPort.isOpen()) {
            serialPort.closePort();
            logger.info("Serial port closed.");
        }
    }

    private static String bytesToHex(byte[] bytes, int length) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < length; i++) {
            sb.append(String.format("%02X ", bytes[i]));
        }
        return sb.toString().trim();
    }
}