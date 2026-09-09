package com.chebysight.chebyagent.android;

import android.content.Context;
import android.os.SystemClock;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyInfo;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.KeyFactory;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.spec.ECGenParameterSpec;
import java.util.UUID;

/** Owner-controlled commerce consent. No PhoneBridge command can change this setting. */
final class CommerceAuthorization {
    static final String PREFS = "cheby_commerce_authorization";
    private static final String KEY_RECEIPT = "owner_checkout_signature";
    private static final String KEY_ALIAS = "cheby_checkout_owner_v1";
    private static final byte[] PROOF = "cheby.checkout.owner-preapproval.v1".getBytes(StandardCharsets.UTF_8);
    private static final long APPROVAL_TTL_MS = 120_000L;
    private static Pending pending;
    private static String approvedFingerprint;
    private static long approvedUntil;
    // Kept true across the system credential screen as well as our own Activity.
    static volatile boolean ownerInteractionActive;

    private CommerceAuthorization() { }

    static boolean isPreapproved(Context context) {
        String receipt = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_RECEIPT, "");
        try {
            return !receipt.isEmpty() && verifyOwnerProof(Base64.decode(receipt, Base64.NO_WRAP));
        } catch (Exception invalid) {
            return false;
        }
    }

    static synchronized boolean disableByOwner(Context context) {
        // Removing the hardware key revokes copied old receipts, too.
        try {
            KeyStore keys = keyStore();
            if (keys.containsAlias(KEY_ALIAS)) keys.deleteEntry(KEY_ALIAS);
        } catch (Exception failure) {
            return false;
        }
        boolean saved = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().remove(KEY_RECEIPT).commit();
        if (saved) {
            pending = null;
            approvedFingerprint = null;
            approvedUntil = 0;
            AuditLog.append(context, "owner_commerce_authorization",
                    JsonUtil.obj("preapproved_checkout", false));
        }
        return saved;
    }

    static synchronized boolean saveOwnerProof(Context context, byte[] proof) throws Exception {
        if (!verifyOwnerProof(proof)) return false;
        boolean saved = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putString(KEY_RECEIPT, Base64.encodeToString(proof, Base64.NO_WRAP)).commit();
        if (saved) AuditLog.append(context, "owner_commerce_authorization",
                JsonUtil.obj("preapproved_checkout", true));
        return saved;
    }

    static Signature beginOwnerProof() throws Exception {
        KeyStore keys = keyStore();
        if (!keys.containsAlias(KEY_ALIAS)) {
            KeyPairGenerator generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore");
            KeyGenParameterSpec.Builder spec = new KeyGenParameterSpec.Builder(KEY_ALIAS,
                    KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY)
                    .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256)
                    .setUserAuthenticationRequired(true);
            if (Build.VERSION.SDK_INT >= 30) {
                spec.setUserAuthenticationParameters(0,
                        KeyProperties.AUTH_BIOMETRIC_STRONG | KeyProperties.AUTH_DEVICE_CREDENTIAL);
            } else {
                spec.setUserAuthenticationValidityDurationSeconds(-1);
            }
            generator.initialize(spec.build());
            generator.generateKeyPair();
        }
        requireOwnerKey(keys);
        Signature signature = Signature.getInstance("SHA256withECDSA");
        signature.initSign((PrivateKey) keys.getKey(KEY_ALIAS, null));
        return signature;
    }

    static byte[] finishOwnerProof(Signature authenticatedSignature, String onceId) throws Exception {
        authenticatedSignature.update(onceId == null ? PROOF
                : ("cheby.checkout.once.v1:" + onceId).getBytes(StandardCharsets.UTF_8));
        return authenticatedSignature.sign();
    }

    private static boolean verifyOwnerProof(byte[] proof) throws Exception {
        KeyStore keys = keyStore();
        if (!keys.containsAlias(KEY_ALIAS)) return false;
        requireOwnerKey(keys);
        Signature verifier = Signature.getInstance("SHA256withECDSA");
        verifier.initVerify(keys.getCertificate(KEY_ALIAS).getPublicKey());
        verifier.update(PROOF);
        return verifier.verify(proof);
    }

    private static void requireOwnerKey(KeyStore keys) throws Exception {
        PrivateKey key = (PrivateKey) keys.getKey(KEY_ALIAS, null);
        KeyInfo info = KeyFactory.getInstance(key.getAlgorithm(), "AndroidKeyStore").getKeySpec(key, KeyInfo.class);
        if (!info.isUserAuthenticationRequired() || info.getUserAuthenticationValidityDurationSeconds() > 0
                || info.getOrigin() != KeyProperties.ORIGIN_GENERATED || !info.isInsideSecureHardware()) {
            throw new IllegalStateException("Owner proof requires a hardware-generated, per-use authenticated key");
        }
    }

    private static KeyStore keyStore() throws Exception {
        KeyStore keys = KeyStore.getInstance("AndroidKeyStore");
        keys.load(null);
        return keys;
    }

    static synchronized boolean consumeApproval(Context context, JSONObject evidence) throws Exception {
        if (isPreapproved(context)) return true;
        String actual = fingerprint(evidence);
        boolean matches = approvalMatches(approvedFingerprint, actual, approvedUntil,
                SystemClock.elapsedRealtime());
        // A one-shot approval cannot survive a changed screen or be replayed.
        approvedFingerprint = null;
        approvedUntil = 0;
        return matches;
    }

    static boolean approvalMatches(String expected, String actual, long expiresAt, long now) {
        return expected != null && expected.equals(actual) && now < expiresAt;
    }

    static synchronized Pending request(JSONObject evidence) throws Exception {
        String fingerprint = fingerprint(evidence);
        long now = SystemClock.elapsedRealtime();
        if (pending == null || now >= pending.expiresAt || !pending.fingerprint.equals(fingerprint)) {
            String label = evidence.optString("text", "").trim();
            if (label.isEmpty()) label = evidence.optString("content_description", "").trim();
            pending = new Pending(UUID.randomUUID().toString(), fingerprint,
                    evidence.optString("active_package", ""), label,
                    evidence.optString("screen_text", ""), now + APPROVAL_TTL_MS);
        }
        return pending;
    }

    static synchronized Pending get(String id) {
        if (pending == null || !pending.id.equals(id) || SystemClock.elapsedRealtime() >= pending.expiresAt) {
            return null;
        }
        return pending;
    }

    static synchronized boolean approveOnce(String id) {
        Pending current = get(id);
        if (current == null) return false;
        approvedFingerprint = current.fingerprint;
        approvedUntil = current.expiresAt;
        pending = null;
        return true;
    }

    private static String fingerprint(JSONObject evidence) throws Exception {
        // Bind consent to the complete observed screen, target, package and coordinates.
        String canonical = evidence.optString("active_package") + "\n"
                + evidence.optString("screen_text") + "\n" + evidence.optString("text") + "\n"
                + evidence.optString("content_description") + "\n" + evidence.optString("view_id") + "\n"
                + evidence.optString("bounds") + "\n" + evidence.optDouble("x") + ":" + evidence.optDouble("y");
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(canonical.getBytes(StandardCharsets.UTF_8));
        StringBuilder out = new StringBuilder();
        for (byte value : digest) out.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return out.toString();
    }

    static final class Pending {
        final String id, fingerprint, packageName, label, screen;
        final long expiresAt;

        Pending(String id, String fingerprint, String packageName, String label, String screen, long expiresAt) {
            this.id = id;
            this.fingerprint = fingerprint;
            this.packageName = packageName;
            this.label = label;
            this.screen = screen;
            this.expiresAt = expiresAt;
        }
    }
}
