"""
ANSI C12.18 protocol constants, ported 1:1 from the openHAB smartmeterosgp binding
(org.openhab.binding.smartmeterosgp.internal.SmartMeterOSGPBindingConstants), so the
standalone tool speaks exactly the same wire protocol as the working openHAB setup.
"""

from enum import IntEnum

# Link-layer control bytes
NACK = 0x15
ACK = 0x06
START = 0xEE
IDENTITY = 0x00

# C12.18 service request IDs
REQUEST_ID_IDENT = 0x20
REQUEST_ID_TERMINATE = 0x21
REQUEST_ID_READ = 0x30
REQUEST_ID_READ_PARTIAL = 0x3F
REQUEST_ID_WRITE = 0x40
REQUEST_ID_WRITE_PARTIAL = 0x4F
REQUEST_ID_LOGON = 0x50
REQUEST_ID_SECURITY = 0x51
REQUEST_ID_LOGOFF = 0x52
REQUEST_ID_NEGOTIATE = 0x60
REQUEST_ID_NEGOTIATE2 = 0x61
REQUEST_ID_WAIT = 0x70


class C1218ResponseCode(IntEnum):
    """Response code returned as the first payload byte of every service reply."""

    Acknowledge = 0                              # ok
    Error = 1
    Service_Not_Supported = 2
    Insufficient_Security_Clearance = 3
    Operation_Not_Possible = 4
    Inappropriate_Action_Requested = 5
    Device_Busy = 6
    Data_Not_Ready = 7
    Data_Locked = 8
    Renegotiate_Request = 9
    Invalid_Service_Sequence_State = 10
    Security_mechanism_error_detected = 11
    Unknown_or_invalid_Called_APTitle_is_received = 12
    Network_timeout_detected = 13
    Node_is_not_reachable = 14
    Request_is_too_large = 15
    Response_is_too_large = 16
    Segmentation_required = 17
    Segmentation_error = 18


class C1218Baudrate(IntEnum):
    """
    The value IS the C12.18 baud-rate code sent in the NEGOTIATE request.
    "Invalid" at index 0 is a deliberate placeholder so Baud_9600 lands on code 6 -
    dropping it shifts every subsequent code by one.
    """

    Invalid = 0
    Baud_300 = 1
    Baud_600 = 2
    Baud_1200 = 3
    Baud_2400 = 4
    Baud_4800 = 5
    Baud_9600 = 6
    Baud_14400 = 7
    Baud_19200 = 8
    Baud_28800 = 9
    Baud_57600 = 10
    Baud_38400 = 11
    Baud_115200 = 12
    Baud_128000 = 13
    Baud_256000 = 14
