package com.smartmeter.osgp;

import com.fazecast.jSerialComm.SerialPort;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Main {
    private static final Logger logger = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) {
        logger.info("Initializing OSGP Standalone Reader...");

        // Scan available hardware serial ports on the host machine/Pi
        SerialPort[] ports = SerialPort.getCommPorts();
        logger.info("Found {} available serial port(s):", ports.length);

        for (SerialPort port : ports) {
            logger.info(" - Port: {} ({})", port.getSystemPortName(), port.getDescriptivePortName());
        }

        logger.info("Setup complete. Ready for OSGP extraction.");
    }
}
