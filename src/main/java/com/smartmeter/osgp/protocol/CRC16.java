package com.smartmeter.osgp.protocol;

/**
 * CRC16 implementation ported from the openHAB smartmeterosgp binding's CRC16.java.
 *
 * This is a reflected, table-based CRC-16 (equivalent to CRC-16/X-25): poly 0x8408
 * (the bit-reversed form of the "normal" 0x1021 CCITT polynomial), used with an
 * initial value of 0xFFFF and a final XOR of 0xFFFF at the call site. The previous
 * standalone implementation used an unrelated non-reflected, bit-by-bit algorithm
 * with poly 0x1021 and no init/XOR - it never produced a CRC the meter would accept.
 */
public class CRC16 {

    public enum Polynom {
        CRC16_CCIT(0x8408); // CCITT/SDLC/HDLC, reflected form: X16+X12+X5+1

        public final int polynom;

        Polynom(int polynom) {
            this.polynom = polynom;
        }
    }

    private final short[] crcTable;

    public CRC16(Polynom polynom) {
        crcTable = genCrc16Table(polynom);
    }

    /**
     * @param data            bytes to calculate the CRC16 over
     * @param length          number of bytes from the start of {@code data} to include
     * @param initialCrcValue initial CRC value (use 0xFFFF to match the meter)
     * @return the CRC16 value (still needs to be XORed with 0xFFFF by the caller)
     */
    public int calculate(byte[] data, int length, int initialCrcValue) {
        int crc = initialCrcValue;
        for (int p = 0; p < length; p++) {
            crc = (crc >> 8) ^ (crcTable[(crc & 0xFF) ^ (data[p] & 0xFF)] & 0xFFFF);
        }
        return crc;
    }

    private short[] genCrc16Table(Polynom polynom) {
        short[] table = new short[256];
        for (int x = 0; x < 256; x++) {
            int w = x;
            for (int i = 0; i < 8; i++) {
                if ((w & 1) != 0) {
                    w = (w >> 1) ^ polynom.polynom;
                } else {
                    w = w >> 1;
                }
            }
            table[x] = (short) w;
        }
        return table;
    }
}