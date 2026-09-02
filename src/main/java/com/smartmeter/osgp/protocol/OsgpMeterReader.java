package com.smartmeter.osgp.protocol;

import com.fazecast.jSerialComm.SerialPort;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

public class OsgpMeterReader {
    private static final Logger logger = LoggerFactory.getLogger(OsgpMeterReader.class);

    private final String portName;
    private final int baudRate;
    private final String password;
    private SerialPort serialPort;

    public OsgpMeterReader(String portName, int baudRate, String password) {
        this.portName = portName;
        this.baudRate = baudRate;
        this.password = password != null ? password : "";
    }

    public boolean connectAndRead() {
        logger.info("Opening serial port: {}", portName);
        serialPort = SerialPort.getCommPort(portName);
        serialPort.setBaudRate(300); // ANSI C12.18 optical initial handshake rate
        serialPort.setNumDataBits(8);
        serialPort.setNumStopBits(1);
        serialPort.setParity(SerialPort.NO_PARITY);
        serialPort.setComPortTimeouts(SerialPort.TIMEOUT_READ_BLOCKING, 2000, 0);

        if (!serialPort.openPort()) {
            logger.error("Failed to open serial port {}", portName);
            return false;
        }

        try {
            logger.info("Port opened. Sending C12.18 Identification (IDENT)...");
            if (!performIdentification()) {
                logger.error("C12.18 Identification failed.");
                return false;
            }

            if (baudRate > 300) {
                logger.info("Negotiating baud rate shift to {}...", baudRate);
                if (negotiateBaudRate(baudRate)) {
                    serialPort.setBaudRate(baudRate);
                    logger.info("Baud rate switched to {}", baudRate);
                }
            }

            if (!password.isEmpty()) {
                logger.info("Sending C12.18 LOGON with password...");
                if (!performLogon(password)) {
                    logger.error("C12.18 LOGON failed! Check your meter password.");
                    return false;
                }
                logger.info("LOGON successful!");
            }

            // Read live energy metrics from Table 23
            readTable23EnergyData();

            return true;
        } catch (Exception e) {
            logger.error("Error communicating with OSGP meter", e);
            return false;
        } finally {
            if (serialPort != null && serialPort.isOpen()) {
                serialPort.closePort();
                logger.info("Serial port closed.");
            }
        }
    }

    private boolean performIdentification() {
        byte[] identPacket = new byte[]{ (byte) 0xEE, 0x00, 0x00, 0x00, 0x00, 0x00 };
        int crc = CRC16.calculate(identPacket, CRC16.Polynom.CRC16_CCITT, 0);
        
        byte[] fullFrame = new byte[8];
        System.arraycopy(identPacket, 0, fullFrame, 0, 6);
        fullFrame[6] = (byte) (crc & 0xFF);
        fullFrame[7] = (byte) ((crc >> 8) & 0xFF);

        serialPort.writeBytes(fullFrame, fullFrame.length);

        byte[] response = new byte[10];
        int read = serialPort.readBytes(response, response.length);
        
        return read > 0 && response[0] == C1218Constants.OK;
    }

    private boolean negotiateBaudRate(int targetBaud) {
        C1218Constants.C1218Baudrate baudEnum = C1218Constants.C1218Baudrate.fromRate(targetBaud);
        byte[] negoPacket = new byte[]{ (byte) 0x21, baudEnum.getCode(), 0x00, 0x00 };
        int crc = CRC16.calculate(negoPacket, CRC16.Polynom.CRC16_CCITT, 0);

        byte[] fullFrame = new byte[6];
        System.arraycopy(negoPacket, 0, fullFrame, 0, 4);
        fullFrame[4] = (byte) (crc & 0xFF);
        fullFrame[5] = (byte) ((crc >> 8) & 0xFF);

        serialPort.writeBytes(fullFrame, fullFrame.length);

        byte[] response = new byte[5];
        int read = serialPort.readBytes(response, response.length);
        return read > 0 && response[0] == C1218Constants.OK;
    }

    private boolean performLogon(String pass) {
        byte[] logonPacket = new byte[23];
        logonPacket[0] = (byte) 0x50; // LOGON command (0x50)
        logonPacket[1] = 0x00;        // User ID High
        logonPacket[2] = 0x01;        // User ID Low

        byte[] passBytes = pass.getBytes(StandardCharsets.US_ASCII);
        for (int i = 0; i < 20; i++) {
            if (i < passBytes.length) {
                logonPacket[3 + i] = passBytes[i];
            } else {
                logonPacket[3 + i] = 0x20; // ASCII space padding
            }
        }

        int crc = CRC16.calculate(logonPacket, CRC16.Polynom.CRC16_CCITT, 0);

        byte[] fullFrame = new byte[25];
        System.arraycopy(logonPacket, 0, fullFrame, 0, 23);
        fullFrame[23] = (byte) (crc & 0xFF);
        fullFrame[24] = (byte) ((crc >> 8) & 0xFF);

        serialPort.writeBytes(fullFrame, fullFrame.length);

        byte[] response = new byte[5];
        int read = serialPort.readBytes(response, response.length);

        return read > 0 && response[0] == C1218Constants.OK;
    }

    private void readTable23EnergyData() {
        logger.info("Reading Table 23 (Forward & Reverse Active Energy)...");
        
        byte[] readTable23Cmd = new byte[]{ (byte) 0x30, 0x00, 0x17, 0x00, 0x00, 0x00, 0x00 };
        int crc = CRC16.calculate(readTable23Cmd, CRC16.Polynom.CRC16_CCITT, 0);

        byte[] fullFrame = new byte[9];
        System.arraycopy(readTable23Cmd, 0, fullFrame, 0, 7);
        fullFrame[7] = (byte) (crc & 0xFF);
        fullFrame[8] = (byte) ((crc >> 8) & 0xFF);

        serialPort.writeBytes(fullFrame, fullFrame.length);

        byte[] rawResponse = new byte[64];
        int bytesRead = serialPort.readBytes(rawResponse, rawResponse.length);

        if (bytesRead <= 0) {
            logger.warn("No response received for Table 23 read.");
            return;
        }

        if (rawResponse[0] != C1218Constants.OK) {
            logger.error("Meter returned response code {} for Table 23 read.", rawResponse[0]);
            return;
        }

        ByteBuffer buffer = ByteBuffer.wrap(rawResponse, 1, bytesRead - 1);
        buffer.order(ByteOrder.LITTLE_ENDIAN);

        int fwdActiveEnergy = buffer.getInt();
        int revActiveEnergy = buffer.getInt();

        logger.info("=========================================");
        logger.info("          METER LIVE READINGS            ");
        logger.info("=========================================");
        logger.info("Forward Active Energy : {} Wh", fwdActiveEnergy);
        logger.info("Reverse Active Energy : {} Wh", revActiveEnergy);
        logger.info("=========================================");
    }
}