package com.chebysight.chebyagent.android;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertSame;

import org.junit.Test;

import java.math.BigInteger;
import java.security.InvalidKeyException;
import java.security.NoSuchAlgorithmException;
import java.security.NoSuchProviderException;
import java.security.Principal;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.SignatureException;
import java.security.cert.CertificateEncodingException;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Date;
import java.util.Set;

import javax.security.auth.x500.X500Principal;

public final class PhoneBridgeTlsStoreTest {
    private static final String ALIAS = "phonebridge-client";
    private static final X500Principal PHONEBRIDGE_CA = new X500Principal("CN=PhoneBridge Test CA");
    private static final X500Principal OTHER_CA = new X500Principal("CN=Other Test CA");

    @Test
    public void selectsIdentityForNullOrEmptyIssuers() {
        PhoneBridgeTlsStore.SingleIdentityKeyManager manager = manager("RSA", PHONEBRIDGE_CA);

        assertArrayEquals(new String[]{ALIAS}, manager.getClientAliases("RSA", null));
        assertArrayEquals(new String[]{ALIAS}, manager.getClientAliases("RSA", new Principal[0]));
        assertEquals(ALIAS, manager.chooseClientAlias(new String[]{"RSA"}, null, null));
        assertEquals(ALIAS, manager.chooseEngineClientAlias(new String[]{"RSA"}, new Principal[0], null));
    }

    @Test
    public void selectsOnlyWhenAcceptableIssuerMatches() {
        PhoneBridgeTlsStore.SingleIdentityKeyManager manager = manager("RSA", PHONEBRIDGE_CA);

        assertEquals(ALIAS, manager.chooseClientAlias(new String[]{"EC", "RSA"}, new Principal[]{PHONEBRIDGE_CA}, null));
        assertEquals(ALIAS, manager.chooseEngineClientAlias(new String[]{"RSA"}, new Principal[]{PHONEBRIDGE_CA}, null));
        assertNull(manager.getClientAliases("RSA", new Principal[]{OTHER_CA}));
        assertNull(manager.chooseClientAlias(new String[]{"RSA"}, new Principal[]{OTHER_CA}, null));
        assertNull(manager.chooseEngineClientAlias(new String[]{"RSA"}, new Principal[]{OTHER_CA}, null));
    }

    @Test
    public void matchesLeafPublicKeyAlgorithm() {
        PhoneBridgeTlsStore.SingleIdentityKeyManager rsa = manager("RSA", PHONEBRIDGE_CA);
        PhoneBridgeTlsStore.SingleIdentityKeyManager ec = manager("EC", PHONEBRIDGE_CA);

        assertEquals(ALIAS, rsa.chooseClientAlias(new String[]{"RSA"}, null, null));
        assertNull(rsa.chooseClientAlias(new String[]{"EC"}, null, null));
        assertNull(rsa.chooseClientAlias(new String[]{"unknown"}, null, null));
        assertNull(rsa.chooseClientAlias(new String[]{null}, null, null));
        assertNull(rsa.chooseClientAlias(null, null, null));
        assertEquals(ALIAS, ec.chooseEngineClientAlias(new String[]{"EC"}, null, null));
        assertNull(ec.chooseEngineClientAlias(new String[]{"RSA"}, null, null));
    }

    @Test
    public void exposesOnlyTheFixedAliasMaterial() {
        PrivateKey privateKey = new FakePrivateKey("RSA");
        X509Certificate certificate = new FakeCertificate("RSA", PHONEBRIDGE_CA);
        PhoneBridgeTlsStore.SingleIdentityKeyManager manager =
                new PhoneBridgeTlsStore.SingleIdentityKeyManager(ALIAS, privateKey, new X509Certificate[]{certificate});

        assertSame(privateKey, manager.getPrivateKey(ALIAS));
        assertNull(manager.getPrivateKey("other"));
        assertArrayEquals(new X509Certificate[]{certificate}, manager.getCertificateChain(ALIAS));
        assertNull(manager.getCertificateChain("other"));
        assertNull(manager.getServerAliases("RSA", null));
        assertNull(manager.chooseServerAlias("RSA", null, null));
        assertNull(manager.chooseEngineServerAlias("RSA", null, null));
    }

    private static PhoneBridgeTlsStore.SingleIdentityKeyManager manager(String algorithm, X500Principal issuer) {
        return new PhoneBridgeTlsStore.SingleIdentityKeyManager(
                ALIAS,
                new FakePrivateKey(algorithm),
                new X509Certificate[]{new FakeCertificate(algorithm, issuer)}
        );
    }

    private static final class FakePrivateKey implements PrivateKey {
        private final String algorithm;

        FakePrivateKey(String algorithm) {
            this.algorithm = algorithm;
        }

        @Override
        public String getAlgorithm() {
            return algorithm;
        }

        @Override
        public String getFormat() {
            return null;
        }

        @Override
        public byte[] getEncoded() {
            return null;
        }
    }

    private static final class FakePublicKey implements PublicKey {
        private final String algorithm;

        FakePublicKey(String algorithm) {
            this.algorithm = algorithm;
        }

        @Override
        public String getAlgorithm() {
            return algorithm;
        }

        @Override
        public String getFormat() {
            return null;
        }

        @Override
        public byte[] getEncoded() {
            return null;
        }
    }

    @SuppressWarnings("deprecation")
    private static final class FakeCertificate extends X509Certificate {
        private final PublicKey publicKey;
        private final X500Principal issuer;

        FakeCertificate(String algorithm, X500Principal issuer) {
            this.publicKey = new FakePublicKey(algorithm);
            this.issuer = issuer;
        }

        @Override public X500Principal getIssuerX500Principal() { return issuer; }
        @Override public X500Principal getSubjectX500Principal() { return new X500Principal("CN=Phone"); }
        @Override public PublicKey getPublicKey() { return publicKey; }
        @Override public void checkValidity() { }
        @Override public void checkValidity(Date date) { }
        @Override public int getVersion() { return 3; }
        @Override public BigInteger getSerialNumber() { return BigInteger.ONE; }
        @Override public Principal getIssuerDN() { return issuer; }
        @Override public Principal getSubjectDN() { return getSubjectX500Principal(); }
        @Override public Date getNotBefore() { return new Date(0); }
        @Override public Date getNotAfter() { return new Date(Long.MAX_VALUE); }
        @Override public byte[] getTBSCertificate() { return new byte[0]; }
        @Override public byte[] getSignature() { return new byte[0]; }
        @Override public String getSigAlgName() { return "none"; }
        @Override public String getSigAlgOID() { return "0.0"; }
        @Override public byte[] getSigAlgParams() { return null; }
        @Override public boolean[] getIssuerUniqueID() { return null; }
        @Override public boolean[] getSubjectUniqueID() { return null; }
        @Override public boolean[] getKeyUsage() { return null; }
        @Override public int getBasicConstraints() { return -1; }
        @Override public byte[] getEncoded() throws CertificateEncodingException { return new byte[0]; }
        @Override public void verify(PublicKey key) throws CertificateException, NoSuchAlgorithmException, InvalidKeyException, NoSuchProviderException, SignatureException { }
        @Override public void verify(PublicKey key, String provider) throws CertificateException, NoSuchAlgorithmException, InvalidKeyException, NoSuchProviderException, SignatureException { }
        @Override public String toString() { return "FakeCertificate"; }
        @Override public Set<String> getCriticalExtensionOIDs() { return null; }
        @Override public Set<String> getNonCriticalExtensionOIDs() { return null; }
        @Override public byte[] getExtensionValue(String oid) { return null; }
        @Override public boolean hasUnsupportedCriticalExtension() { return false; }
    }
}
