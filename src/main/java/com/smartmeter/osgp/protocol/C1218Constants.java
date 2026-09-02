package com.smartmeter.osgp.protocol;

public class C1218Constants {
    public static final byte STP = (byte) 0xEE; // Start character for C12.18 packets
    public static final byte ACK = 0x06;
    public static final byte NAK = 0x15;

    public enum C1218Baudrate {
        B300(0x00, 300),
        B600(0x01, 600),
        B1200(0x02, 1200),
        B2400(0x03, 2400),
        B4800(0x04, 4800),
        B9600(0x05, 9600),
        B19200(0x06, 19200),
        B28800(0x07, 28800);

        private final byte code;
        private final int baudrate;

        C1218Baudrate(int code, int baudrate) {
            this.code = (byte) code;
            this.baudrate = baudrate;
        }

        public byte getCode() {
            return code;
        }

        public int getBaudrate() {
            return baudrate;
        }

        public static C1218Baudrate fromBaudrate(int baudrate) {
            for (C1218Baudrate b : values()) {
                if (b.getBaudrate() == baudrate) {
                    return b;
                }
            }
            return B9600;
        }
    }
}