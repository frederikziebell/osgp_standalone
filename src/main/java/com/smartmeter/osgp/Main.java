package com.smartmeter.osgp;

import com.smartmeter.osgp.protocol.OsgpMeterReader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Main {
    private static final Logger logger = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) {
        logger.info("Starting Standalone Smart Meter Reader...");

        String port = args.length > 0 ? args[0] : "/dev/ttyUSB0";
        int baud = args.length > 1 ? Integer.parseInt(args[1]) : 9600;
        String password = args.length > 2 ? args[2] : "";

        if (password.isEmpty()) {
            logger.warn("No password provided. C12.18 LOGON step will be skipped.");
        }

        OsgpMeterReader reader = new OsgpMeterReader(port, baud, password);
        reader.start();
    }
}