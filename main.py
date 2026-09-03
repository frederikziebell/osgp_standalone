#!/usr/bin/env python3
"""
Entry point for the standalone OSGP smart meter reader.

Usage: python3 main.py [config.properties]
"""

import logging
import os
import signal
import sys

try:
    from meter_reader import OsgpMeterReader
except ImportError as e:
    if "serial" not in str(e):
        raise
    sys.stderr.write("pyserial is not installed.\n"
                     "Install it with:  sudo apt install python3-serial\n"
                     "             or:  %s -m pip install -r requirements.txt\n"
                     % sys.executable)
    sys.exit(1)

PLACEHOLDER_PASSWORD = "YOUR_20_CHAR_ASCII_KEY_HERE"

logger = logging.getLogger("Main")


def load_properties(path):
    """Reads a java.util.Properties-style 'key=value' file.

    Supports '#'/'!' comment lines, blank lines, ':' as an alternative separator and
    CRLF line endings. Backslash escapes and line continuations (which Java supports but
    this config never uses) are not handled.
    """
    props = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f.read().splitlines():
            line = raw_line.lstrip()
            if not line or line[0] in "#!":
                continue
            eq = line.find("=")
            colon = line.find(":")
            if eq < 0:
                sep = colon
            elif colon < 0:
                sep = eq
            else:
                sep = min(eq, colon)
            if sep < 0:
                continue
            key = line[:sep].strip()
            # Like Java, strip leading whitespace from the value but keep trailing.
            value = line[sep + 1:].lstrip()
            props[key] = value
    return props


def require(props, key, config_path):
    value = props.get(key)
    if value is None or not value.strip():
        sys.stderr.write("Missing required setting '%s' in %s\n" % (key, config_path))
        sys.exit(1)
    return value


def parse_int(props, key, default_value):
    value = props.get(key)
    if value is None or not value.strip():
        return default_value
    try:
        return int(value.strip())
    except ValueError:
        sys.stderr.write("Setting '%s' must be a number, got '%s' - using default %d\n"
                         % (key, value, default_value))
        return default_value


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.properties"

    if not os.access(config_path, os.R_OK):
        sys.stderr.write("Config file not found: %s\n" % os.path.abspath(config_path))
        sys.stderr.write("Copy config.properties.example to %s and fill in your meter's "
                         "password.\n" % config_path)
        sys.exit(1)

    try:
        props = load_properties(config_path)
    except OSError as e:
        sys.stderr.write("Could not read config file %s: %s\n" % (config_path, e))
        sys.exit(1)

    # Equivalent of slf4j-simple's -Dorg.slf4j.simpleLogger.defaultLogLevel property.
    log_level = logging.getLevelName(props.get("logLevel", "INFO").strip().upper())
    if not isinstance(log_level, int):
        sys.stderr.write("Unknown logLevel '%s' - using INFO\n" % props.get("logLevel"))
        log_level = logging.INFO
    logging.basicConfig(stream=sys.stderr, level=log_level,
                        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s")

    port = require(props, "port", config_path)
    password = require(props, "password", config_path)
    if password == PLACEHOLDER_PASSWORD:
        sys.stderr.write("Please edit %s and set your real meter password.\n" % config_path)
        sys.exit(1)

    username = props.get("username", "OpenHAB")
    baud = parse_int(props, "baud", 9600)
    user_id = parse_int(props, "userId", 1)
    refresh_interval_seconds = parse_int(props, "refreshIntervalSeconds", 2)
    logoff_interval_seconds = parse_int(props, "logoffIntervalSeconds", 540)
    idle_start_time = props.get("idleStartTime", "02:10:00")
    idle_seconds = parse_int(props, "idleSeconds", 480)

    logger.info("Starting Standalone Smart Meter Reader (config: %s)...", config_path)

    try:
        reader = OsgpMeterReader(port, baud, user_id, username, password,
                                 refresh_interval_seconds, logoff_interval_seconds,
                                 idle_start_time, idle_seconds)
    except ValueError as e:
        sys.stderr.write("Invalid setting in %s: %s\n" % (config_path, e))
        sys.exit(1)

    def handle_signal(signum, _frame):
        logger.info("Shutting down, logging off from meter... (signal %d)", signum)
        reader.request_stop()

    # Replaces the JVM shutdown hook. Unlike the hook, this lets the main loop finish its
    # cycle and run the logoff/terminate exchange before the process exits.
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    reader.connect_and_run()


if __name__ == "__main__":
    main()
