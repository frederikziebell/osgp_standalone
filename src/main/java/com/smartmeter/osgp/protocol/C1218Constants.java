package com.smartmeter.osgp.protocol;

public class C1218Constants {

    // C12.18 Response Codes
    public static final byte NOK = 0x01;
    public static final byte OK = 0x00;
    public static final byte ERR_ON_OPEN = 0x02;
    public static final byte ERR_ON_READ = 0x03;

    // ANSI C12.18 Baudrates
    public enum C1218Baudrate {
        BAUD_300(300, (byte) 0x00),
        BAUD_600(600, (byte) 0x01),
        BAUD_1200(1200, (byte) 0x02),
        BAUD_2400(2400, (byte) 0x03),
        BAUD_4800(4800, (byte) 0x04),
        BAUD_9600(9600, (byte) 0x05),
        BAUD_19200(19200, (byte) 0x06),
        BAUD_28800(28800, (byte) 0x07);

        private final int rate;
        private final byte code;

        C1218Baudrate(int rate, byte code) {
            this.rate = rate;
            this.code = code;
        }

        public int getRate() {
            return rate;
        }

        public byte getCode() {
            return code;
        }

        public static C1218Baudrate fromRate(int rate) {
            for (C1218Baudrate b : values()) {
                if (b.getRate() == rate) {
                    return b;
                }
            }
            return BAUD_9600; // Default fallback
        }
    }
}
