package com.smartmeter.osgp.protocol;

/**
 * ANSI C12.18 protocol constants, ported 1:1 from the openHAB smartmeterosgp binding
 * (org.openhab.binding.smartmeterosgp.internal.SmartMeterOSGPBindingConstants), so the
 * standalone tool speaks exactly the same wire protocol as your working openHAB setup.
 */
public final class C1218Constants {

    private C1218Constants() {
    }

    // Link-layer control bytes
    public static final byte NACK = 0x15;
    public static final byte ACK = 0x06;
    public static final byte START = (byte) 0xEE;
    public static final byte IDENTITY = 0;

    // C12.18 service request IDs
    public static final byte RequestID_Ident = 0x20;
    public static final byte RequestID_Terminate = 0x21;
    public static final byte RequestID_Read = 0x30;
    public static final byte RequestID_ReadPartial = 0x3f;
    public static final byte RequestID_Write = 0x40;
    public static final byte RequestID_WritePartial = 0x4f;
    public static final byte RequestID_Logon = 0x50;
    public static final byte RequestID_Security = 0x51;
    public static final byte RequestID_Logoff = 0x52;
    public static final byte RequestID_Negotiate = 0x60;
    public static final byte RequestID_Negotiate2 = 0x61;
    public static final byte RequestID_Wait = 0x70;

    /** Response code returned as the first payload byte of every service reply. */
    public enum C1218ResponseCode {
        Acknowledge,                              // 0: ok
        Error,                                    // 1
        Service_Not_Supported,                    // 2
        Insufficient_Security_Clearance,          // 3
        Operation_Not_Possible,                   // 4
        Inappropriate_Action_Requested,           // 5
        Device_Busy,                              // 6
        Data_Not_Ready,                           // 7
        Data_Locked,                              // 8
        Renegotiate_Request,                      // 9
        Invalid_Service_Sequence_State,           // 10
        Security_mechanism_error_detected,
        Unknown_or_invalid_Called_APTitle_is_received,
        Network_timeout_detected,
        Node_is_not_reachable,
        Request_is_too_large,
        Response_is_too_large,
        Segmentation_required,
        Segmentation_error
    }

    /**
     * Ordinal position IS the C12.18 baud-rate code sent in the NEGOTIATE request.
     * "Invalid" at index 0 is a deliberate placeholder so Baud_9600 lands on code 6 -
     * dropping it (as the old standalone code did) shifts every subsequent code by one.
     */
    public enum C1218Baudrate {
        Invalid,
        Baud_300,
        Baud_600,
        Baud_1200,
        Baud_2400,
        Baud_4800,
        Baud_9600,
        Baud_14400,
        Baud_19200,
        Baud_28800,
        Baud_57600,
        Baud_38400,
        Baud_115200,
        Baud_128000,
        Baud_256000
    }
}