package com.chebysight.chebyagent.android;

import android.content.Context;
import android.security.keystore.KeyProperties;
import android.security.keystore.KeyProtection;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.Principal;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Enumeration;

import javax.crypto.Cipher;
import javax.net.ssl.KeyManager;
import javax.net.ssl.SSLEngine;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509ExtendedKeyManager;

final class PhoneBridgeTlsStore {
    private static final String CA_CERT_FILE = "phonebridge_ca.pem";
    private static final String CLIENT_PKCS12_FILE = "phonebridge_client.p12";
    private static final String CLIENT_PKCS12_PASSWORD_FILE = "phonebridge_client_password.txt";
    private static final String ANDROID_KEYSTORE = "AndroidKeyStore";
    private static final String CLIENT_KEY_ALIAS = "chebynode_phonebridge_client_v2";
    private static final String LEGACY_CLIENT_KEY_ALIAS = "chebynode_phonebridge_client_v1";
    private static volatile String clientIdentityDecision = "not_loaded";

    static final class ClientIdentityUpgradeRequiredException extends Exception {
        ClientIdentityUpgradeRequiredException() {
            super("PhoneBridge client identity must be provisioned again.");
        }
    }

    private PhoneBridgeTlsStore() {
    }

    static synchronized SSLSocketFactory socketFactory(Context context) throws Exception {
        migrateClientIdentity(context);
        File caFile = file(context, CA_CERT_FILE);
        String clientAlias = selectTlsCapableAlias();
        boolean clientIdentity = clientAlias != null;
        if (!caFile.isFile() && !clientIdentity) {
            return (SSLSocketFactory) SSLSocketFactory.getDefault();
        }

        KeyManager[] keyManagers = loadKeyManagers(clientAlias);
        SSLContext sslContext = SSLContext.getInstance("TLS");
        sslContext.init(keyManagers, loadTrustManagers(caFile), null);
        return sslContext.getSocketFactory();
    }

    static synchronized void authenticatedHandshakeCompleted(Context context) throws Exception {
        if (selectTlsCapableAlias() != null) deleteBootstrapFiles(context);
    }

    static synchronized JSONObject status(Context context) {
        File caFile = file(context, CA_CERT_FILE);
        boolean clientIdentity = false;
        boolean storedIdentity = false;
        try {
            clientIdentity = selectTlsCapableAlias() != null;
            storedIdentity = hasAnyStoredIdentity();
        } catch (Exception ignored) {
            // Status stays fail-closed when Keystore access fails.
        }
        boolean bootstrapPending = file(context, CLIENT_PKCS12_FILE).isFile()
                || file(context, CLIENT_PKCS12_PASSWORD_FILE).isFile();
        return JsonUtil.obj(
                "custom_ca_configured", caFile.isFile(),
                "client_certificate_configured", clientIdentity,
                "client_certificate_password_configured", false,
                "mutual_tls_configured", caFile.isFile() && clientIdentity,
                "client_key_system_backed", clientIdentity,
                "client_identity_bootstrap_pending", bootstrapPending,
                "client_identity_upgrade_required", !clientIdentity && storedIdentity
        );
    }

    static String clientIdentityDecision() {
        return clientIdentityDecision;
    }

    static void beginTlsAttempt() {
        clientIdentityDecision = "attempt_started";
    }

    private static TrustManager[] loadTrustManagers(File caFile) throws Exception {
        if (!caFile.isFile()) return null;
        CertificateFactory factory = CertificateFactory.getInstance("X.509");
        X509Certificate certificate;
        try (FileInputStream input = new FileInputStream(caFile)) {
            certificate = (X509Certificate) factory.generateCertificate(input);
        }

        KeyStore trustStore = KeyStore.getInstance(KeyStore.getDefaultType());
        trustStore.load(null, null);
        trustStore.setCertificateEntry("phonebridge-ca", certificate);

        TrustManagerFactory factoryManager = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        factoryManager.init(trustStore);
        return factoryManager.getTrustManagers();
    }

    private static KeyManager[] loadKeyManagers(String selectedAlias) throws Exception {
        if (selectedAlias == null) return null;
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        KeyStore.Entry entry = keyStore.getEntry(selectedAlias, null);
        if (!(entry instanceof KeyStore.PrivateKeyEntry)) {
            throw new IllegalStateException("Android Keystore client identity is not a private-key entry");
        }
        KeyStore.PrivateKeyEntry privateEntry = (KeyStore.PrivateKeyEntry) entry;
        if (privateEntry.getPrivateKey() == null
                || privateEntry.getCertificateChain() == null
                || privateEntry.getCertificateChain().length == 0) {
            throw new IllegalStateException("Android Keystore client identity is incomplete");
        }
        validatePrivateKeyTlsCapability(privateEntry.getPrivateKey());
        clientIdentityDecision = "loaded_"
                + safeKeyType(privateEntry.getCertificate().getPublicKey().getAlgorithm())
                + "_chain_" + privateEntry.getCertificateChain().length;
        return new KeyManager[]{new SingleIdentityKeyManager(selectedAlias, privateEntry)};
    }

    static final class SingleIdentityKeyManager extends X509ExtendedKeyManager {
        private final String alias;
        private final PrivateKey privateKey;
        private final X509Certificate[] certificateChain;

        SingleIdentityKeyManager(String alias, KeyStore.PrivateKeyEntry entry) {
            Certificate[] chain = entry.getCertificateChain();
            X509Certificate[] x509Chain = new X509Certificate[chain.length];
            for (int index = 0; index < chain.length; index++) {
                if (!(chain[index] instanceof X509Certificate)) {
                    throw new IllegalStateException("Android Keystore client certificate chain is not X.509");
                }
                x509Chain[index] = (X509Certificate) chain[index];
            }
            this.alias = alias;
            this.privateKey = entry.getPrivateKey();
            this.certificateChain = x509Chain;
        }

        SingleIdentityKeyManager(String alias, PrivateKey privateKey, X509Certificate[] certificateChain) {
            if (alias == null || alias.isEmpty() || privateKey == null || certificateChain == null || certificateChain.length == 0) {
                throw new IllegalArgumentException("Client identity is incomplete");
            }
            this.alias = alias;
            this.privateKey = privateKey;
            this.certificateChain = certificateChain.clone();
        }

        @Override
        public String[] getClientAliases(String keyType, Principal[] issuers) {
            boolean selected = supports(keyType, issuers);
            clientIdentityDecision = decision(selected, new String[]{keyType}, issuers);
            return selected ? new String[]{alias} : null;
        }

        @Override
        public String chooseClientAlias(String[] keyTypes, Principal[] issuers, Socket socket) {
            if (keyTypes != null) {
                for (String keyType : keyTypes) {
                    if (supports(keyType, issuers)) {
                        clientIdentityDecision = decision(true, keyTypes, issuers);
                        return alias;
                    }
                }
            }
            clientIdentityDecision = decision(false, keyTypes, issuers);
            return null;
        }

        @Override
        public String chooseEngineClientAlias(String[] keyTypes, Principal[] issuers, SSLEngine engine) {
            return chooseClientAlias(keyTypes, issuers, null);
        }

        @Override
        public X509Certificate[] getCertificateChain(String requestedAlias) {
            if (!alias.equals(requestedAlias)) return null;
            markIdentityAccess("chain");
            return certificateChain.clone();
        }

        @Override
        public PrivateKey getPrivateKey(String requestedAlias) {
            if (!alias.equals(requestedAlias)) return null;
            markIdentityAccess("key");
            return privateKey;
        }

        @Override
        public String[] getServerAliases(String keyType, Principal[] issuers) {
            return null;
        }

        @Override
        public String chooseServerAlias(String keyType, Principal[] issuers, Socket socket) {
            return null;
        }

        @Override
        public String chooseEngineServerAlias(String keyType, Principal[] issuers, SSLEngine engine) {
            return null;
        }

        private boolean supports(String keyType, Principal[] acceptableIssuers) {
            if (keyType == null
                    || !certificateChain[0].getPublicKey().getAlgorithm().equalsIgnoreCase(keyType)) {
                return false;
            }
            if (acceptableIssuers == null || acceptableIssuers.length == 0) return true;
            for (X509Certificate certificate : certificateChain) {
                Principal issuer = certificate.getIssuerX500Principal();
                for (Principal acceptableIssuer : acceptableIssuers) {
                    if (issuer.equals(acceptableIssuer)) return true;
                }
            }
            return false;
        }

        private String decision(boolean selected, String[] keyTypes, Principal[] issuers) {
            StringBuilder summary = new StringBuilder(selected ? "selected" : "rejected");
            summary.append("_types_");
            if (keyTypes == null || keyTypes.length == 0) {
                summary.append("none");
            } else {
                int limit = Math.min(keyTypes.length, 4);
                for (int index = 0; index < limit; index++) {
                    if (index > 0) summary.append('-');
                    summary.append(safeKeyType(keyTypes[index]));
                }
                if (keyTypes.length > limit) summary.append("-more");
            }
            summary.append("_issuers_").append(issuers == null ? "null" : issuers.length);
            summary.append("_leaf_").append(safeKeyType(certificateChain[0].getPublicKey().getAlgorithm()));
            return summary.toString();
        }
    }

    private static String safeKeyType(String keyType) {
        if (keyType == null || keyType.isEmpty()) return "unknown";
        String sanitized = keyType.replaceAll("[^A-Za-z0-9_-]", "_");
        return sanitized.length() <= 24 ? sanitized : sanitized.substring(0, 24);
    }

    private static synchronized void markIdentityAccess(String stage) {
        String suffix = "_access_" + stage;
        if (!clientIdentityDecision.contains(suffix)) clientIdentityDecision += suffix;
    }

    private static void validatePrivateKeyTlsCapability(PrivateKey privateKey) throws Exception {
        String algorithm = privateKey.getAlgorithm();
        if ("RSA".equalsIgnoreCase(algorithm)) {
            validateRsaPrivateCipher(privateKey, "RSA/ECB/NoPadding");
            validateRsaPrivateCipher(privateKey, "RSA/ECB/PKCS1Padding");
            validateSignature(privateKey, "SHA256withRSA");
        } else if ("EC".equalsIgnoreCase(algorithm)) {
            validateSignature(privateKey, "SHA256withECDSA");
        } else {
            throw new IllegalStateException("Unsupported PhoneBridge client private-key algorithm");
        }
    }

    private static void validateRsaPrivateCipher(PrivateKey privateKey, String transformation) throws Exception {
        Cipher cipher = Cipher.getInstance(transformation);
        cipher.init(Cipher.ENCRYPT_MODE, privateKey);
        if (cipher.doFinal(new byte[]{0x43, 0x68, 0x65, 0x62, 0x79}).length == 0) {
            throw new IllegalStateException("Android Keystore RSA TLS capability self-check failed");
        }
    }

    private static void validateSignature(PrivateKey privateKey, String signatureAlgorithm) throws Exception {
        Signature signature = Signature.getInstance(signatureAlgorithm);
        signature.initSign(privateKey);
        signature.update(new byte[]{0x43, 0x68, 0x65, 0x62, 0x79});
        if (signature.sign().length == 0) {
            throw new IllegalStateException("Android Keystore client identity signing self-check failed");
        }
    }

    private static void migrateClientIdentity(Context context) throws Exception {
        File clientFile = file(context, CLIENT_PKCS12_FILE);
        if (!clientFile.isFile()) {
            if (selectTlsCapableAlias() == null && hasAnyStoredIdentity()) {
                throw new ClientIdentityUpgradeRequiredException();
            }
            return;
        }
        char[] password = readClientCertificatePassword(context);
        KeyStore source = KeyStore.getInstance("PKCS12");
        try (FileInputStream input = new FileInputStream(clientFile)) {
            source.load(input, password);
        }
        Enumeration<String> aliases = source.aliases();
        String sourceAlias = null;
        while (aliases.hasMoreElements()) {
            String candidate = aliases.nextElement();
            if (source.isKeyEntry(candidate)) {
                sourceAlias = candidate;
                break;
            }
        }
        if (sourceAlias == null) throw new IllegalStateException("PKCS#12 has no client private key");
        PrivateKey privateKey = (PrivateKey) source.getKey(sourceAlias, password);
        Certificate[] chain = source.getCertificateChain(sourceAlias);
        if (privateKey == null || chain == null || chain.length == 0) {
            throw new IllegalStateException("PKCS#12 client identity is incomplete");
        }

        importIdentity(LEGACY_CLIENT_KEY_ALIAS, privateKey, chain);
        importIdentity(CLIENT_KEY_ALIAS, privateKey, chain);
    }

    private static void importIdentity(String alias, PrivateKey privateKey, Certificate[] chain) throws Exception {
        KeyStore target = KeyStore.getInstance(ANDROID_KEYSTORE);
        target.load(null);
        KeyProtection.Builder protection = new KeyProtection.Builder(
                KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_ENCRYPT
        );
        protection.setDigests(
                KeyProperties.DIGEST_NONE,
                KeyProperties.DIGEST_SHA256,
                KeyProperties.DIGEST_SHA384,
                KeyProperties.DIGEST_SHA512
        );
        if ("RSA".equalsIgnoreCase(privateKey.getAlgorithm())) {
            protection.setSignaturePaddings(
                    KeyProperties.SIGNATURE_PADDING_RSA_PKCS1,
                    KeyProperties.SIGNATURE_PADDING_RSA_PSS
            );
            // Android 10 Conscrypt delegates opaque RSA TLS signatures through
            // Cipher.ENCRYPT_MODE with the private key. Those calls require the
            // encrypt purpose and raw/PKCS#1 cipher paddings in Android Keystore.
            protection.setEncryptionPaddings(
                    KeyProperties.ENCRYPTION_PADDING_NONE,
                    KeyProperties.ENCRYPTION_PADDING_RSA_PKCS1
            );
            protection.setRandomizedEncryptionRequired(false);
        }
        target.setEntry(
                alias,
                new KeyStore.PrivateKeyEntry(privateKey, chain),
                protection.build()
        );
        if (!target.containsAlias(alias)) {
            throw new IllegalStateException("Android Keystore rejected the client identity");
        }
        KeyStore.Entry imported = target.getEntry(alias, null);
        if (!(imported instanceof KeyStore.PrivateKeyEntry)) {
            throw new IllegalStateException("Android Keystore client identity is not a private-key entry");
        }
        validatePrivateKeyTlsCapability(((KeyStore.PrivateKeyEntry) imported).getPrivateKey());
    }

    private static String selectTlsCapableAlias() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        for (String alias : new String[]{CLIENT_KEY_ALIAS, LEGACY_CLIENT_KEY_ALIAS}) {
            if (!keyStore.isKeyEntry(alias)) continue;
            try {
                KeyStore.Entry entry = keyStore.getEntry(alias, null);
                if (!(entry instanceof KeyStore.PrivateKeyEntry)) continue;
                validatePrivateKeyTlsCapability(((KeyStore.PrivateKeyEntry) entry).getPrivateKey());
                return alias;
            } catch (Exception ignored) {
                // A legacy signing-only RSA key remains available for rollback,
                // but must never be selected for a TLS path it cannot perform.
            }
        }
        return null;
    }

    private static boolean hasAnyStoredIdentity() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        return hasAnyStoredIdentity(keyStore);
    }

    private static boolean hasAnyStoredIdentity(KeyStore keyStore) throws Exception {
        return keyStore.isKeyEntry(CLIENT_KEY_ALIAS) || keyStore.isKeyEntry(LEGACY_CLIENT_KEY_ALIAS);
    }

    private static char[] readClientCertificatePassword(Context context) throws Exception {
        File passwordFile = file(context, CLIENT_PKCS12_PASSWORD_FILE);
        if (!passwordFile.isFile()) return new char[0];
        byte[] bytes;
        try (FileInputStream input = new FileInputStream(passwordFile)) {
            bytes = readAllBytes(input);
        }
        return new String(bytes, StandardCharsets.UTF_8).trim().toCharArray();
    }

    private static void deleteBootstrapFiles(Context context) {
        deleteRequired(file(context, CLIENT_PKCS12_FILE));
        deleteRequired(file(context, CLIENT_PKCS12_PASSWORD_FILE));
    }

    private static void deleteRequired(File file) {
        if (file.exists() && !file.delete()) {
            throw new IllegalStateException("Failed to remove temporary TLS bootstrap credential");
        }
    }

    private static byte[] readAllBytes(InputStream input) throws Exception {
        byte[] buffer = new byte[8192];
        int read;
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        while ((read = input.read(buffer)) >= 0) output.write(buffer, 0, read);
        return output.toByteArray();
    }

    private static File file(Context context, String name) {
        return new File(context.getFilesDir(), name);
    }
}
