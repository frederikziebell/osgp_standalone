package com.smartmeter.osgp;

import com.smartmeter.osgp.protocol.OsgpMeterReader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Main {
    private static final Logger logger = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) {
        logger.info("Starting OSGP Standalone Reader...");

        // Configurable parameters (Defaulting to /dev/ttyUSB0 and 9600 baud)
        String port = args.length > 0 ? args[0] : "/dev/ttyUSB0";
        int baud = args.length > 1 ? Integer.parseInt(args[1]) : 9600;

        OsgpMeterReader reader = new OsgpMeterReader(port, baud);
        reader.connectAndRead();
    }
}