package com.cheby.codex.updater;

import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

final class InstallPolicy {
    private static final Pattern APK_FILE = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,126}\\.apk");
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");

    private InstallPolicy() {}

    static Command validateCommand(
            String expectedPackage,
            String requestedPackage,
            String relativeFileName,
            String expectedSha256) {
        if (!expectedPackage.equals(requestedPackage)) {
            throw new IllegalArgumentException("unexpected target package");
        }
        if (relativeFileName == null || !APK_FILE.matcher(relativeFileName).matches()) {
            throw new IllegalArgumentException("invalid update file name");
        }
        String normalizedSha = expectedSha256 == null
                ? ""
                : expectedSha256.toLowerCase(Locale.ROOT);
        if (!SHA256.matcher(normalizedSha).matches()) {
            throw new IllegalArgumentException("invalid SHA-256");
        }
        return new Command(requestedPackage, relativeFileName, normalizedSha);
    }

    static void requireAllowedSigner(
            Set<String> archiveSigners,
            Set<String> installedSigners,
            String pinnedSigner) {
        if (archiveSigners == null || archiveSigners.size() != 1) {
            throw new IllegalArgumentException("APK must have exactly one current signer");
        }
        if (installedSigners != null && !installedSigners.isEmpty()) {
            if (!archiveSigners.equals(installedSigners)) {
                throw new IllegalArgumentException("APK signer does not match installed app");
            }
            return;
        }
        if (!archiveSigners.contains(pinnedSigner.toLowerCase(Locale.ROOT))) {
            throw new IllegalArgumentException("APK signer is not the pinned Cheby signer");
        }
    }

    static void requireNonDowngrade(long archiveVersionCode, Long installedVersionCode) {
        if (installedVersionCode != null && archiveVersionCode < installedVersionCode) {
            throw new IllegalArgumentException("APK version is older than the installed app");
        }
    }

    record Command(String packageName, String relativeFileName, String sha256) {}
}
