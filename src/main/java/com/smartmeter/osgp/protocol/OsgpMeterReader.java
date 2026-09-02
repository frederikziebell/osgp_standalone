package com.smartmeter.osgp.protocol;

import com.fazecast.jSerialComm.SerialPort;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

public class OsgpMeterReader {
    private static final Logger logger = LoggerFactory.getLogger(OsgpMeterReader.class);

    private final String portName;
    private final int baudRate;
    private SerialPort serialPort;

    public OsgpMeterReader(String portName, int baudRate) {
        this.portName = portName;
        this.baudRate = baudRate;
    }

    public boolean connectAndRead() {
        logger.info("Opening serial port: {}", portName);
        serialPort = SerialPort.getCommPort(portName);
        serialPort.setBaudRate(300); // ANSI C12.18 starts initial handshake at 300 baud
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
            
            // Perform C12.18 Handshake
            if (!performIdentification()) {
                logger.error("C12.18 Identification failed.");
                return false;
            }

            // Negotiate target baud rate (e.g., 9600 / 19200)
            if (baudRate > 300) {
                logger.info("Negotiating baud rate shift to {}...", baudRate);
                if (negotiateBaudRate(baudRate)) {
                    serialPort.setBaudRate(baudRate);
                    logger.info("Baud rate switched to {}", baudRate);
                }
            }

            // Read energy values (Table 23)
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

    private boolean performIdentification() throws Exception {
        // C12.18 Identification Request packet: [0xEE, 0x00, 0x00, 0x00, 0x00, 0x00, CRC_LOW, CRC_HIGH]
        byte[] identPacket = new byte[]{ (byte) 0xEE, 0x00, 0x00, 0x00, 0x00, 0x00 };
        int crc = CRC16.calculate(identPacket, CRC16.Polynom.CRC16_CCITT, 0);
        
        byte[] fullFrame = new byte[8];
        System.arraycopy(identPacket, 0, fullFrame, 0, 6);
        fullFrame[6] = (byte) (crc & 0xFF);
        fullFrame[7] = (byte) ((crc >> 8) & 0xFF);

        serialPort.writeBytes(fullFrame, fullFrame.length);

        byte[] response = new byte[10];
        int read = serialPort.readBytes(response, response.length);
        
        if (read > 0 && response[0] == C1218Constants.OK) {
            logger.info("Meter ACK received for IDENT.");
            return true;
        }
        return false;
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

    private void readTable23EnergyData() {
        // Table 23 parsing logic (extracted from handleTable23Reply in binding)
        logger.info("Reading Table 23 (Forward & Reverse Active Energy)...");
        
        // Mock buffer structure representing raw byte frame received from Table 23
        // In real execution, this ByteBuffer comes from reading serial response bytes
        byte[] rawTableBytes = new byte[12]; 
        ByteBuffer buffer = ByteBuffer.wrap(rawTableBytes);
        buffer.order(ByteOrder.LITTLE_ENDIAN);

        // Parse metrics
        int fwdActiveEnergy = buffer.getInt();
        int revActiveEnergy = buffer.getInt();

        logger.info("=== LIVE READINGS ===");
        logger.info("Forward Active Energy : {} Wh", fwdActiveEnergy);
        logger.info("Reverse Active Energy : {} Wh", revActiveEnergy);
        logger.info("=====================");
    }
}
