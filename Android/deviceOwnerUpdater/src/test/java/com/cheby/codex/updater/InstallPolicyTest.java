package com.cheby.codex.updater;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.util.Set;

import org.junit.Test;

public final class InstallPolicyTest {
    private static final String SHA = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    @Test
    public void validCommandIsNormalized() {
        InstallPolicy.Command command = InstallPolicy.validateCommand(
                "com.termux",
                "com.termux",
                "ChebyCodex-0.7.1.apk",
                SHA.toUpperCase());

        assertEquals("com.termux", command.packageName());
        assertEquals(SHA, command.sha256());
    }

    @Test
    public void traversalFileNameIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> InstallPolicy.validateCommand(
                "com.termux", "com.termux", "../payload.apk", SHA));
    }

    @Test
    public void wrongPackageIsRejected() {
        assertThrows(IllegalArgumentException.class, () -> InstallPolicy.validateCommand(
                "com.termux", "other.package", "payload.apk", SHA));
    }

    @Test
    public void installedSignerMustMatchExactly() {
        assertThrows(IllegalArgumentException.class, () -> InstallPolicy.requireAllowedSigner(
                Set.of("new"), Set.of("installed"), "new"));
        InstallPolicy.requireAllowedSigner(Set.of("installed"), Set.of("installed"), "other");
    }

    @Test
    public void firstInstallRequiresPinnedSigner() {
        assertThrows(IllegalArgumentException.class, () -> InstallPolicy.requireAllowedSigner(
                Set.of("unexpected"), Set.of(), "pinned"));
        InstallPolicy.requireAllowedSigner(Set.of("pinned"), Set.of(), "pinned");
    }

    @Test
    public void downgradeIsRejectedButReinstallIsAllowed() {
        assertThrows(IllegalArgumentException.class, () ->
                InstallPolicy.requireNonDowngrade(9, 10L));
        InstallPolicy.requireNonDowngrade(10, 10L);
        InstallPolicy.requireNonDowngrade(1, null);
    }
}
