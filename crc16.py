"""
CRC16 implementation ported from the openHAB smartmeterosgp binding's CRC16.java.

This is a reflected, table-based CRC-16 (equivalent to CRC-16/X-25): poly 0x8408
(the bit-reversed form of the "normal" 0x1021 CCITT polynomial), used with an
initial value of 0xFFFF and a final XOR of 0xFFFF at the call site.
"""

# CCITT/SDLC/HDLC, reflected form: X16+X12+X5+1
CRC16_CCIT = 0x8408


def _gen_crc16_table(polynom):
    table = []
    for x in range(256):
        w = x
        for _ in range(8):
            if w & 1:
                w = (w >> 1) ^ polynom
            else:
                w = w >> 1
        table.append(w & 0xFFFF)
    return table


class CRC16:
    def __init__(self, polynom=CRC16_CCIT):
        self._crc_table = _gen_crc16_table(polynom)

    def calculate(self, data, length=None, initial_crc_value=0xFFFF):
        """
        :param data: bytes/bytearray to calculate the CRC16 over
        :param length: number of bytes from the start of ``data`` to include
        :param initial_crc_value: initial CRC value (use 0xFFFF to match the meter)
        :return: the CRC16 value (still needs to be XORed with 0xFFFF by the caller)
        """
        if length is None:
            length = len(data)
        crc = initial_crc_value
        for p in range(length):
            crc = (crc >> 8) ^ self._crc_table[(crc & 0xFF) ^ data[p]]
        return crc
