package com.smartmeter.osgp.crypto;

import java.security.Security;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

import org.bouncycastle.jce.provider.BouncyCastleProvider;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class OsgpSecurity {
    private static final Logger logger = LoggerFactory.getLogger(OsgpSecurity.class);

    static {
        // Register Bouncy Castle crypto provider
        Security.addProvider(new BouncyCastleProvider());
    }

    /**
     * Decrypts an OSGP AES-GCM encrypted byte payload.
     *
     * @param encryptedData Encrypted byte payload from meter table
     * @param hexKey        32-character Hex Encryption Key (16 bytes / 128 bits)
     * @param iv            Initialization Vector / Nonce (typically 12 bytes derived from counter)
     * @return Decrypted plain byte array
     */
    public static byte[] decryptGcm(byte[] encryptedData, String hexKey, byte[] iv) throws Exception {
        byte[] keyBytes = hexToBytes(hexKey);
        SecretKeySpec keySpec = new SecretKeySpec(keyBytes, "AES");
        
        // GCM Authentication Tag size is typically 128 bits (16 bytes) or 96 bits
        GCMParameterSpec gcmSpec = new GCMParameterSpec(128, iv);

        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding", "BC");
        cipher.init(Cipher.DECRYPT_MODE, keySpec, gcmSpec);

        return cipher.doFinal(encryptedData);
    }

    /**
     * Utility method to convert Hexadecimal string key configuration into byte arrays.
     */
    public static byte[] hexToBytes(String hex) {
        int len = hex.length();
        byte[] data = new byte[len / 2];
        for (int i = 0; i < len; i += 2) {
            data[i / 2] = (byte) ((Character.digit(hex.charAt(i), 16) << 4)
                                 + Character.digit(hex.charAt(i + 1), 16));
        }
        return data;
    }
}