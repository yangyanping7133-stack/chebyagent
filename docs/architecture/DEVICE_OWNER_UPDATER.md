# Cheby device-owner updater

## Purpose

This isolated Android application is the future local update authority for the
dedicated ALN phone. It does not depend on Huawei AppGallery and does not weaken
APK identity checks. It accepts an update only when all of these conditions hold:

- the updater is the active device owner;
- the ADB command comes through a receiver protected by `android.permission.DUMP`;
- the requested package is exactly `com.termux`;
- the file is a single APK inside the updater's private external update directory;
- the supplied SHA-256 matches the file;
- the APK package name matches; and
- the APK current signer matches the installed app, or the pinned Cheby signer
  for a first install; and
- the APK version is not older than the installed app.

The update is then submitted through `PackageInstaller` with policy install
reason and `USER_ACTION_NOT_REQUIRED`. A submitted session is not counted as an
installed update until the package installer returns `STATUS_SUCCESS`.

## Current acceptance boundary

The module can be built and tested without changing the phone. Activating it as
device owner is intentionally a separate operation because the repository rules
require explicit Founder authorization for permission escalation or device reset.
Do not run `dpm set-device-owner`, remove accounts, reset, or reprovision the ALN
phone as part of ordinary build verification.

## Future clean-device flow

After a separately approved clean-device provisioning:

1. Install and activate `com.cheby.codex.updater/.ChebyDeviceAdminReceiver` as
   device owner before adding user accounts.
2. Send the protected `PREPARE` command once; it creates the private update
   directory and records `ready`.
3. Push a signed APK to
   `/sdcard/Android/data/com.cheby.codex.updater/files/updates/<name>.apk`.
4. Send the explicit local ADB broadcast with the exact file name, package name,
   and SHA-256.
5. Read the protected local status provider and independently verify the target
   package version, signer, and preserved application data.

Build success, session submission, and installation success are separate gates.
