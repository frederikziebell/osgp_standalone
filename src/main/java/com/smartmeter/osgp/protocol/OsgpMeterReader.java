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
        this.baudRate = baudRate > 0 ? baudRate : 9600;
        this.password = password != null ? password : "";
    }

    public boolean connectAndRead() {
        logger.info("Opening serial port: {} at {} baud", portName, baudRate);
        serialPort = SerialPort.getCommPort(portName);
        
        // Exact serial port config matching openHAB smartmeterosgp binding
        serialPort.setBaudRate(baudRate);
        serialPort.setNumDataBits(8);
        serialPort.setNumStopBits(1);
        serialPort.setParity(SerialPort.EVEN_PARITY); // EVEN PARITY is required for OSGP C12.18 optical ports!
        serialPort.setComPortTimeouts(SerialPort.TIMEOUT_READ_BLOCKING, 3000, 0);

        if (!serialPort.openPort()) {
            logger.error("Failed to open serial port {}", portName);
            return false;
        }

        // Power the optical probe transceiver circuit
        serialPort.setDTR();
        serialPort.setRTS();

        try {
            // Wake up optical receiver
            wakeUpOpticalPort();

            logger.info("Sending C12.18 Identification (IDENT)...");
            if (!performIdentification()) {
                logger.warn("IDENT failed with EVEN parity. Retrying with NO parity...");
                serialPort.setParity(SerialPort.NO_PARITY);
                wakeUpOpticalPort();
                if (!performIdentification()) {
                    logger.error("C12.18 Identification failed on both parity settings.");
                    return false;
                }
            }

            logger.info("IDENT acknowledged by meter!");

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

    private void wakeUpOpticalPort() throws InterruptedException {
        // Preamble 0x55 / 0xEE to trigger optical phototransistor AGC
        byte[] wakeup = new byte[]{ (byte) 0xEE, (byte) 0xEE };
        serialPort.writeBytes(wakeup, wakeup.length);
        Thread.sleep(150); // Give the optical head 150ms to wake up
    }

    private boolean performIdentification() {
        // C12.18 IDENT service request (0xEE) frame
        byte[] identPacket = new byte[]{ (byte) 0xEE, 0x00, 0x00, 0x00, 0x00, 0x00 };
        int crc = CRC16.calculate(identPacket, CRC16.Polynom.CRC16_CCITT, 0);
        
        byte[] fullFrame = new byte[8];
        System.arraycopy(identPacket, 0, fullFrame, 0, 6);
        fullFrame[6] = (byte) (crc & 0xFF);
        fullFrame[7] = (byte) ((crc >> 8) & 0xFF);

        serialPort.writeBytes(fullFrame, fullFrame.length);

        byte[] response = new byte[10];
        int read = serialPort.readBytes(response, response.length);
        
        return read > 0 && (response[0] == C1218Constants.OK || response[0] == (byte) 0x00);
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
                logonPacket[3 + i] = 0x20; // Space padding
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

        return read > 0 && (response[0] == C1218Constants.OK || response[0] == (byte) 0x00);
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

        if (rawResponse[0] != C1218Constants.OK && rawResponse[0] != 0x00) {
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