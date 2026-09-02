package com.smartmeter.osgp.protocol;

/**
 * CRC16 calculation utility for C12.18 framing.
 */
public class CRC16 {

    public enum Polynom {
        CRC16_CCITT(0x1021),
        CRC16_BUYPASS(0x8005);

        private final int value;

        Polynom(int value) {
            this.value = value;
        }

        public int getValue() {
            return value;
        }
    }

    public static int calculate(byte[] bytes, Polynom polynom, int initialValue) {
        int crc = initialValue;
        int poly = polynom.getValue();

        for (byte b : bytes) {
            for (int i = 0; i < 8; i++) {
                boolean bit = ((b >> (7 - i) & 1) == 1);
                boolean c15 = ((crc >> 15 & 1) == 1);
                crc <<= 1;
                if (c15 ^ bit) {
                    crc ^= poly;
                }
            }
        }
        return crc & 0xFFFF;
    }
}
