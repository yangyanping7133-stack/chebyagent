package com.cheby.codex.updater;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

final class ApkVerifier {
    private ApkVerifier() {}

    static VerifiedApk verify(Context context, File updatesDirectory, InstallPolicy.Command command)
            throws IOException, PackageManager.NameNotFoundException {
        File root = updatesDirectory.getCanonicalFile();
        File apk = new File(root, command.relativeFileName()).getCanonicalFile();
        if (!root.equals(apk.getParentFile()) || !apk.isFile()) {
            throw new IOException("update APK is outside the private update directory");
        }
        if (!command.sha256().equals(sha256(apk))) {
            throw new IOException("update APK SHA-256 mismatch");
        }

        PackageManager packageManager = context.getPackageManager();
        PackageInfo archive = packageManager.getPackageArchiveInfo(
                apk.getAbsolutePath(),
                PackageManager.GET_SIGNING_CERTIFICATES);
        if (archive == null || !command.packageName().equals(archive.packageName)) {
            throw new IOException("update APK package name mismatch");
        }
        Set<String> archiveSigners = signerDigests(archive);
        Set<String> installedSigners = Set.of();
        Long installedVersionCode = null;
        try {
            PackageInfo installed = packageManager.getPackageInfo(
                    command.packageName(),
                    PackageManager.GET_SIGNING_CERTIFICATES);
            installedSigners = signerDigests(installed);
            installedVersionCode = installed.getLongVersionCode();
        } catch (PackageManager.NameNotFoundException ignored) {
            // A first install is allowed only when the archive matches the pinned project signer.
        }
        InstallPolicy.requireAllowedSigner(
                archiveSigners,
                installedSigners,
                BuildConfig.TARGET_SIGNER_SHA256);
        InstallPolicy.requireNonDowngrade(archive.getLongVersionCode(), installedVersionCode);
        return new VerifiedApk(apk, archive.getLongVersionCode());
    }

    private static Set<String> signerDigests(PackageInfo packageInfo) throws IOException {
        if (packageInfo.signingInfo == null) {
            throw new IOException("APK signing information is unavailable");
        }
        Signature[] signatures = packageInfo.signingInfo.getApkContentsSigners();
        if (signatures == null) {
            throw new IOException("APK has no current signing certificate");
        }
        Set<String> digests = new HashSet<>();
        Arrays.stream(signatures)
                .map(Signature::toByteArray)
                .map(ApkVerifier::sha256)
                .forEach(digests::add);
        return Collections.unmodifiableSet(digests);
    }

    private static String sha256(File file) throws IOException {
        MessageDigest digest = newDigest();
        try (FileInputStream input = new FileInputStream(file)) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) > 0) {
                digest.update(buffer, 0, count);
            }
        }
        return hex(digest.digest());
    }

    private static String sha256(byte[] value) {
        MessageDigest digest = newDigest();
        return hex(digest.digest(value));
    }

    private static MessageDigest newDigest() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException impossible) {
            throw new AssertionError(impossible);
        }
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) {
            result.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return result.toString();
    }

    record VerifiedApk(File file, long versionCode) {}
}
