package com.smartmeter.osgp;

import com.smartmeter.osgp.protocol.OsgpMeterReader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class Main {
    private static final Logger logger = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) {
        if (args.length < 3) {
            System.err.println(
                    "Usage: <port> <baud> <password> [username] [userId] [refreshSeconds] [logoffSeconds]");
            System.err.println("Example: /dev/ttyUSB0 9600 MY20CHARASCIIKEY0123 OpenHAB 1 2 540");
            System.exit(1);
        }

        try {
            String port = args[0];
            int baud = Integer.parseInt(args[1]);
            String password = args[2];
            // These match openHAB's own defaults, since you left everything default there.
            String username = args.length > 3 ? args[3] : "OpenHAB";
            int userId = args.length > 4 ? Integer.parseInt(args[4]) : 1;
            int refreshIntervalSeconds = args.length > 5 ? Integer.parseInt(args[5]) : 2;
            int logoffIntervalSeconds = args.length > 6 ? Integer.parseInt(args[6]) : 540;

            logger.info("Starting Standalone Smart Meter Reader...");

            OsgpMeterReader reader = new OsgpMeterReader(port, baud, userId, username, password,
                    refreshIntervalSeconds, logoffIntervalSeconds, "02:10:00", 480);

            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                logger.info("Shutting down, logging off from meter...");
                reader.requestStop();
            }));

            reader.connectAndRun();
        } catch (NumberFormatException e) {
            System.err.println("baud/userId/refreshSeconds/logoffSeconds must all be numbers: " + e.getMessage());
            System.exit(1);
        }
    }
}