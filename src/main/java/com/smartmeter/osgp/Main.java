package com.smartmeter.osgp;

import com.smartmeter.osgp.protocol.OsgpMeterReader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Main {
    private static final Logger logger = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) {
        logger.info("Starting OSGP Standalone Reader...");

        String port = args.length > 0 ? args[0] : "/dev/ttyUSB0";
        int baud = args.length > 1 ? Integer.parseInt(args[1]) : 9600;
        
        // OSGP 128-bit Encryption & Authentication Keys (Hex formatted)
        String encryptionKey = args.length > 2 ? args[2] : "";
        String authenticationKey = args.length > 3 ? args[3] : "";

        if (encryptionKey.isEmpty()) {
            logger.warn("No OSGP Encryption Key provided. Running in unencrypted mode.");
        }

        OsgpMeterReader reader = new OsgpMeterReader(port, baud);
        reader.connectAndRead();
    }
}