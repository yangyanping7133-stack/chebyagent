package com.chebysight.chebyagent.android;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.io.File;
import java.security.KeyStore;
import java.security.SecureRandom;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class PairingStore {
    private static final String PREFS = "chebyagent_pairing";
    private static final String LEGACY_KEY_TOKEN = "pairing_token";
    private static final String KEY_CIPHERTEXT = "pairing_token_ciphertext";
    private static final String KEY_IV = "pairing_token_iv";
    private static final String KEYSTORE = "AndroidKeyStore";
    private static final String KEY_ALIAS = "chebynode_pairing_token_aes_v1";

    private PairingStore() {
    }

    static synchronized String getOrCreateToken(Context context) {
        try {
            SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            String ciphertext = prefs.getString(KEY_CIPHERTEXT, null);
            String iv = prefs.getString(KEY_IV, null);
            if ((ciphertext == null) != (iv == null)) {
                throw new IllegalStateException("Encrypted pairing credential is incomplete");
            }
            if (ciphertext != null) {
                String decrypted = decrypt(ciphertext, iv);
                if (decrypted.length() < 24) throw new IllegalStateException("Pairing credential is invalid");
                deleteLegacyDebugExport(context);
                return decrypted;
            }

            String token = prefs.getString(LEGACY_KEY_TOKEN, null);
            if (token == null || token.length() < 24) {
                byte[] random = new byte[32];
                new SecureRandom().nextBytes(random);
                token = Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
            }
            storeEncrypted(prefs, token);
            deleteLegacyDebugExport(context);
            return token;
        } catch (Exception error) {
            throw new IllegalStateException("Pairing credential is unavailable from Android Keystore", error);
        }
    }

    private static void storeEncrypted(SharedPreferences prefs, String token) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
        byte[] ciphertext = cipher.doFinal(token.getBytes(java.nio.charset.StandardCharsets.UTF_8));
        boolean committed = prefs.edit()
                .putString(KEY_CIPHERTEXT, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .putString(KEY_IV, Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
                .remove(LEGACY_KEY_TOKEN)
                .commit();
        if (!committed) throw new IllegalStateException("Failed to persist encrypted pairing credential");
    }

    private static String decrypt(String ciphertext, String iv) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(
                Cipher.DECRYPT_MODE,
                getOrCreateKey(),
                new GCMParameterSpec(128, Base64.decode(iv, Base64.DEFAULT))
        );
        return new String(
                cipher.doFinal(Base64.decode(ciphertext, Base64.DEFAULT)),
                java.nio.charset.StandardCharsets.UTF_8
        );
    }

    private static SecretKey getOrCreateKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(KEYSTORE);
        keyStore.load(null);
        java.security.Key existing = keyStore.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) return (SecretKey) existing;

        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
        ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }

    private static void deleteLegacyDebugExport(Context context) {
        File legacy = new File(context.getFilesDir(), "pairing_token.txt");
        if (legacy.exists() && !legacy.delete()) {
            throw new IllegalStateException("Failed to remove legacy pairing credential export");
        }
    }
}
