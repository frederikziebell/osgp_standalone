package com.smartmeter.osgp;

import com.smartmeter.osgp.protocol.OsgpMeterReader;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

public class Main {
    private static final Logger logger = LoggerFactory.getLogger(Main.class);
    private static final String PLACEHOLDER_PASSWORD = "YOUR_20_CHAR_ASCII_KEY_HERE";

    public static void main(String[] args) {
        Path configPath = Path.of(args.length > 0 ? args[0] : "config.properties");

        if (!Files.isReadable(configPath)) {
            System.err.println("Config file not found: " + configPath.toAbsolutePath());
            System.err.println(
                    "Copy config.properties.example to " + configPath + " and fill in your meter's password.");
            System.exit(1);
            return;
        }

        Properties props = new Properties();
        try (FileInputStream in = new FileInputStream(configPath.toFile())) {
            props.load(in);
        } catch (IOException e) {
            System.err.println("Could not read config file " + configPath + ": " + e.getMessage());
            System.exit(1);
            return;
        }

        String port = require(props, "port", configPath);
        String password = require(props, "password", configPath);
        if (PLACEHOLDER_PASSWORD.equals(password)) {
            System.err.println("Please edit " + configPath + " and set your real meter password.");
            System.exit(1);
            return;
        }

        String username = props.getProperty("username", "OpenHAB");
        int baud = parseInt(props, "baud", 9600);
        int userId = parseInt(props, "userId", 1);
        int refreshIntervalSeconds = parseInt(props, "refreshIntervalSeconds", 2);
        int logoffIntervalSeconds = parseInt(props, "logoffIntervalSeconds", 540);
        String idleStartTime = props.getProperty("idleStartTime", "02:10:00");
        int idleSeconds = parseInt(props, "idleSeconds", 480);

        logger.info("Starting Standalone Smart Meter Reader (config: {})...", configPath);

        OsgpMeterReader reader = new OsgpMeterReader(port, baud, userId, username, password, refreshIntervalSeconds,
                logoffIntervalSeconds, idleStartTime, idleSeconds);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            logger.info("Shutting down, logging off from meter...");
            reader.requestStop();
        }));

        reader.connectAndRun();
    }

    private static String require(Properties props, String key, Path configPath) {
        String value = props.getProperty(key);
        if (value == null || value.isBlank()) {
            System.err.println("Missing required setting '" + key + "' in " + configPath);
            System.exit(1);
        }
        return value;
    }

    private static int parseInt(Properties props, String key, int defaultValue) {
        String value = props.getProperty(key);
        if (value == null || value.isBlank()) {
            return defaultValue;
        }
        try {
            return Integer.parseInt(value.trim());
        } catch (NumberFormatException e) {
            System.err.println(
                    "Setting '" + key + "' must be a number, got '" + value + "' - using default " + defaultValue);
            return defaultValue;
        }
    }
}