package com.termux.app;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.SystemClock;

import com.cheby.codex.runtime.EmbeddedRuntime;
import com.cheby.codex.runtime.EmbeddedRuntimeImportResult;
import com.cheby.codex.runtime.EmbeddedRuntimeStartResult;
import com.cheby.codex.runtime.EmbeddedRuntimeStatus;
import com.chebysight.chebyagent.android.AgentNodeService;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.json.JSONException;
import org.json.JSONObject;

/** Owns bootstrap, provisioning, Codex startup, and the embedded phone executor. */
public final class TermuxEmbeddedRuntime implements EmbeddedRuntime {
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern ASSET_MANIFEST_LINE = Pattern.compile(
        "([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,127})"
    );
    private static final Set<String> REQUIRED_ASSETS = Set.of(
        "ace_memory.py",
        "ace-core-__init__.py",
        "ace-core-skillbook.py",
        "ace-core-insight_source.py",
        "ace-core-LICENSE",
        "ace-core-UPSTREAM.md",
        "mobile-experience-instructions.md",
        "coffee-poster-reference.md",
        "cian-rental-report-reference.md",
        "codex-linux-arm64.tgz",
        "codex-base-instructions.md",
        "debian-rootfs-aarch64.tar.zst",
        "enter-debian.sh",
        "local_mcp.py",
        "install-codex-overlay.py",
        "provider-launcher.py",
        "glm-chat-adapter.py",
        "phonebridge.mjs",
        "provision-guest.sh",
        "provision-runtime.sh",
        "runtime.lock",
        "skill-russia-menu-assistant.md",
        "skill-phone-ui-recovery.md",
        "skill-phone-ui-recovery.openai.yaml",
        "skill-yandex-maps-restaurant-finder.md",
        "skill-yandex-maps-restaurant-finder.openai.yaml",
        "skill-yandex-maps-supermarket-finder.md",
        "skill-yandex-maps-supermarket-finder.openai.yaml",
        "skill-russia-menu-assistant.openai.yaml",
        "skill-yandex-go-taxi-booker.md",
        "skill-yandex-go-taxi-booker.openai.yaml",
        "skill-yandex-maps-coffee-finder.md",
        "skill-yandex-maps-coffee-finder.openai.yaml",
        "skill-yandex-maps-route-planner.md",
        "skill-yandex-maps-route-planner.openai.yaml",
        "skill-yandex-maps-haircut-finder.md",
        "skill-yandex-maps-haircut-finder.openai.yaml",
        "skill-yandex-maps-massage-finder.md",
        "skill-yandex-maps-massage-finder.openai.yaml",
        "skill-yandex-maps-dog-grooming.md",
        "skill-yandex-maps-dog-grooming.openai.yaml",
        "skill-cian-rental-finder.md",
        "skill-cian-rental-finder.openai.yaml",
        "skill-yandex-go-food-order.md",
        "skill-yandex-go-food-order.openai.yaml",
        "start-codex-appserver.sh",
        "start-phonebridge.sh",
        "termux-proot-overlay-aarch64.tar.zst"
    );
    private static final Set<String> PROVISIONING_BUNDLES = Set.of(
        "codex-linux-arm64.tgz",
        "debian-rootfs-aarch64.tar.zst",
        "termux-proot-overlay-aarch64.tar.zst"
    );
    private static final String TERMUX_PREFIX = "/data/data/com.termux/files/usr";
    private static final String TERMUX_HOME = "/data/data/com.termux/files/home";
    private static final String ENTER_DEBIAN = TERMUX_PREFIX + "/bin/cheby-enter-debian";
    private static final String BASH = TERMUX_PREFIX + "/bin/bash";
    private static final String PROVISION_ROOT = TERMUX_HOME + "/.cheby/provision";
    private static final String PROVISION_ASSETS = PROVISION_ROOT + "/assets";
    private static final String PROVISION_SCRIPT = PROVISION_ASSETS + "/provision-runtime.sh";
    private static final String READY_MARKER = TERMUX_HOME + "/.cheby/runtime/ready-4.1.0-dev32";
    private static final String FAILED_MARKER = TERMUX_HOME + "/.cheby/runtime/provision-failed";
    private static final String DEBIAN_ROOT =
        TERMUX_PREFIX + "/var/lib/cheby-runtime/debian-rootfs";
    private static final String CODEX_AUTH = DEBIAN_ROOT + "/root/.codex/auth.json";
    private static final String CODEX_TOKEN_VERIFIER =
        DEBIAN_ROOT + "/root/.cheby/appserver/token.sha256";
    private static final int MAX_AUTH_JSON_BYTES = 1024 * 1024;
    private static final long CODEX_START_DEDUP_WINDOW_MS = 30_000L;
    private static final String START_CODEX = "/opt/cheby/bin/start-codex-appserver";
    private static final String ACTION_EXECUTE = "com.termux.service_execute";
    private static final String EXTRA_ARGUMENTS = "com.termux.execute.arguments";
    private static final String EXTRA_WORKDIR = "com.termux.execute.cwd";
    private static final String EXTRA_BACKGROUND = "com.termux.execute.background";
    private static final String EXTRA_LABEL = "com.termux.execute.command_label";

    private final Context context;
    private final ProviderSettingsStore providerStore;
    private final ScheduledExecutorService monitor;
    private final AtomicBoolean monitorStarted = new AtomicBoolean();
    private boolean preparingBootstrap;
    private boolean bootstrapReady;
    private boolean runtimeReady;
    private boolean provisioning;
    private boolean failed;
    private String pendingTokenSha256;
    private long lastCodexStartRequestElapsedMs;
    private String lastCodexStartTokenSha256;

    public TermuxEmbeddedRuntime(Context context) {
        this.context = context.getApplicationContext();
        this.providerStore = new ProviderSettingsStore(this.context);
        ThreadFactory threadFactory = runnable -> {
            Thread thread = new Thread(runnable, "cheby-runtime-monitor");
            thread.setDaemon(true);
            return thread;
        };
        monitor = Executors.newSingleThreadScheduledExecutor(threadFactory);
    }

    /** Restores an already provisioned appliance after process death or device boot. */
    void resumeIfProvisioned() {
        if (!new File(READY_MARKER).isFile() || !new File(BASH).canExecute()) return;
        synchronized (this) {
            if (runtimeReady || provisioning || failed) return;
            bootstrapReady = true;
            provisioning = true;
        }
        monitor.execute(() -> {
            try {
                stageBundledAssets(false);
                String storedTokenSha256 = readStoredTokenSha256();
                synchronized (TermuxEmbeddedRuntime.this) {
                    if (pendingTokenSha256 == null) pendingTokenSha256 = storedTokenSha256;
                }
                markRuntimeReady();
            } catch (IOException | RuntimeException error) {
                markFailed();
            }
        });
    }

    @Override
    public void prepare(Activity activity) {
        startEmbeddedNode();
        synchronized (this) {
            if (preparingBootstrap || bootstrapReady) return;
            preparingBootstrap = true;
        }
        TermuxInstaller.setupBootstrapIfNeeded(activity, () -> {
            synchronized (TermuxEmbeddedRuntime.this) {
                bootstrapReady = true;
                preparingBootstrap = false;
            }
            prepareProvisionedRuntime();
        });
    }

    @Override
    public EmbeddedRuntimeStartResult ensureStarted(String tokenSha256) {
        if (tokenSha256 == null || !SHA256.matcher(tokenSha256).matches()) {
            return EmbeddedRuntimeStartResult.FAILED;
        }
        synchronized (this) {
            pendingTokenSha256 = tokenSha256.toLowerCase(Locale.US);
            if (failed) return EmbeddedRuntimeStartResult.FAILED;
            if (!runtimeReady) return EmbeddedRuntimeStartResult.PREPARING;
        }
        return startCodex(tokenSha256);
    }

    @Override
    public synchronized EmbeddedRuntimeStatus status() {
        if (failed) return EmbeddedRuntimeStatus.FAILED;
        if (runtimeReady) {
            try {
                return providerStore.configured() ? EmbeddedRuntimeStatus.READY : EmbeddedRuntimeStatus.READY_NEEDS_AUTH;
            } catch (Exception error) {
                return EmbeddedRuntimeStatus.READY_NEEDS_AUTH;
            }
        }
        if (provisioning) return EmbeddedRuntimeStatus.INSTALLING;
        return EmbeddedRuntimeStatus.PREPARING;
    }

    @Override
    public String providerSettings() {
        try { return providerStore.publicSettings(); }
        catch (Exception error) { return "{\"error\":\"无法读取安全配置，请重试。\"}"; }
    }

    @Override
    public synchronized String saveProviderSettings(String json) {
        try {
            providerStore.save(json);
            if (runtimeReady) {
                providerStore.stage(new File(DEBIAN_ROOT));
                if (pendingTokenSha256 != null && providerStore.configured()) startCodex(pendingTokenSha256);
            }
            return null;
        } catch (IllegalArgumentException error) {
            return error.getMessage();
        } catch (Exception error) {
            return "配置保存或应用失败，请检查输入并重试。";
        }
    }

    @Override
    public synchronized String rebuildProviderSettings() {
        try {
            providerStore.rebuild(new File(DEBIAN_ROOT));
            return null;
        } catch (IllegalArgumentException error) {
            return error.getMessage();
        } catch (Exception error) {
            return "无法重新建立安全配置，请稍后重试。已有任务数据未被清除。";
        }
    }

    @Override
    public EmbeddedRuntimeImportResult importCodexAuth(Context ignored, Uri uri) {
        synchronized (this) {
            if (!runtimeReady) return EmbeddedRuntimeImportResult.NOT_READY;
        }
        byte[] authJson = null;
        try {
            authJson = readBounded(uri);
            if (!isCodexAuthJson(authJson)) return EmbeddedRuntimeImportResult.INVALID;
            installCodexAuth(authJson);
            return EmbeddedRuntimeImportResult.IMPORTED;
        } catch (AuthJsonInvalidException | CharacterCodingException | JSONException error) {
            return EmbeddedRuntimeImportResult.INVALID;
        } catch (IOException | RuntimeException error) {
            return EmbeddedRuntimeImportResult.FAILED;
        } finally {
            if (authJson != null) java.util.Arrays.fill(authJson, (byte) 0);
        }
    }

    private void prepareProvisionedRuntime() {
        try {
            boolean alreadyProvisioned = new File(READY_MARKER).isFile();
            stageBundledAssets(!alreadyProvisioned);
            if (alreadyProvisioned) {
                markRuntimeReady();
                return;
            }
            synchronized (this) {
                if (provisioning) return;
                provisioning = true;
                failed = false;
            }
            new File(FAILED_MARKER).delete();
            EmbeddedRuntimeStartResult result = startFixedCommand(
                BASH,
                new String[] {PROVISION_SCRIPT},
                "ChebyCodex runtime setup"
            );
            if (result == EmbeddedRuntimeStartResult.FAILED) {
                markFailed();
                return;
            }
            startProvisionMonitor();
        } catch (IOException | RuntimeException error) {
            markFailed();
        }
    }

    private void startProvisionMonitor() {
        if (!monitorStarted.compareAndSet(false, true)) return;
        monitor.scheduleWithFixedDelay(() -> {
            if (new File(READY_MARKER).isFile()) {
                markRuntimeReady();
                monitor.shutdown();
                return;
            }
            if (new File(FAILED_MARKER).isFile()) {
                markFailed();
                monitor.shutdown();
            }
        }, 1, 1, TimeUnit.SECONDS);
    }

    private void markRuntimeReady() {
        String pending;
        synchronized (this) {
            runtimeReady = true;
            provisioning = false;
            failed = false;
            pending = pendingTokenSha256;
        }
        if (pending != null) startCodex(pending);
    }

    private synchronized void markFailed() {
        provisioning = false;
        failed = true;
    }

    private void stageBundledAssets(boolean includeProvisioningBundles) throws IOException {
        Map<String, String> expected = readAssetManifest();
        if (!expected.keySet().equals(REQUIRED_ASSETS)) {
            throw new IOException("Embedded runtime asset set is invalid");
        }
        File assetRoot = new File(PROVISION_ASSETS);
        if (!assetRoot.isDirectory() && !assetRoot.mkdirs()) {
            throw new IOException("Embedded runtime asset directory is unavailable");
        }
        setOwnerOnly(assetRoot, true);
        for (Map.Entry<String, String> entry : expected.entrySet()) {
            if (!includeProvisioningBundles && PROVISIONING_BUNDLES.contains(entry.getKey())) {
                continue;
            }
            copyVerifiedAsset(entry.getKey(), entry.getValue(), assetRoot);
        }
        copyAssetAtomically("runtime-assets.sha256", assetRoot, null);
    }

    private Map<String, String> readAssetManifest() throws IOException {
        Map<String, String> result = new LinkedHashMap<>();
        try (
            InputStream input = context.getAssets().open(
                "cheby-runtime/runtime-assets.sha256"
            );
            BufferedReader reader = new BufferedReader(
                new InputStreamReader(input, StandardCharsets.US_ASCII)
            )
        ) {
            String line;
            while ((line = reader.readLine()) != null) {
                Matcher matcher = ASSET_MANIFEST_LINE.matcher(line);
                if (!matcher.matches() || result.put(matcher.group(2), matcher.group(1)) != null) {
                    throw new IOException("Embedded runtime asset manifest is invalid");
                }
            }
        }
        return result;
    }

    private static String readStoredTokenSha256() throws IOException {
        File verifier = new File(CODEX_TOKEN_VERIFIER);
        if (!verifier.isFile() || Files.isSymbolicLink(verifier.toPath())) return null;
        try (
            BufferedReader reader = new BufferedReader(
                new InputStreamReader(new FileInputStream(verifier), StandardCharsets.US_ASCII)
            )
        ) {
            String value = reader.readLine();
            if (value == null || reader.readLine() != null) return null;
            return SHA256.matcher(value).matches() ? value : null;
        }
    }

    private void copyVerifiedAsset(String name, String expectedSha256, File assetRoot)
        throws IOException {
        File target = new File(assetRoot, name);
        if (target.isFile() && expectedSha256.equals(fileSha256(target))) return;
        copyAssetAtomically(name, assetRoot, expectedSha256);
    }

    private void copyAssetAtomically(String name, File assetRoot, String expectedSha256)
        throws IOException {
        File target = new File(assetRoot, name);
        File temporary = new File(assetRoot, "." + name + ".part");
        try (
            InputStream input = context.getAssets().open("cheby-runtime/" + name);
            FileOutputStream output = new FileOutputStream(temporary, false)
        ) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                if (count > 0) output.write(buffer, 0, count);
            }
            output.getFD().sync();
        }
        setOwnerOnly(temporary, false);
        if (expectedSha256 != null && !expectedSha256.equals(fileSha256(temporary))) {
            temporary.delete();
            throw new IOException("Embedded runtime asset digest is invalid");
        }
        if (target.exists() && !target.delete()) {
            temporary.delete();
            throw new IOException("Embedded runtime asset could not be replaced");
        }
        if (!temporary.renameTo(target)) {
            temporary.delete();
            throw new IOException("Embedded runtime asset could not be activated");
        }
    }

    private static void setOwnerOnly(File file, boolean executable) throws IOException {
        if (
            !file.setReadable(false, false) ||
            !file.setWritable(false, false) ||
            !file.setExecutable(false, false) ||
            !file.setReadable(true, true) ||
            !file.setWritable(true, true) ||
            (executable && !file.setExecutable(true, true))
        ) {
            throw new IOException("Embedded runtime asset permissions could not be set");
        }
    }

    private static String fileSha256(File file) throws IOException {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
        try (FileInputStream input = new FileInputStream(file)) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                if (count > 0) digest.update(buffer, 0, count);
            }
        }
        StringBuilder result = new StringBuilder(64);
        for (byte value : digest.digest()) result.append(String.format("%02x", value & 0xff));
        return result.toString();
    }

    private void startEmbeddedNode() {
        Intent intent = new Intent(context, AgentNodeService.class)
            .putExtra("start_reason", "cheby_appliance_launch");
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent);
            } else {
                context.startService(intent);
            }
        } catch (RuntimeException ignored) {
            // The sticky receiver and the next foreground launch retry this fixed service.
        }
    }

    private byte[] readBounded(Uri uri) throws IOException, AuthJsonInvalidException {
        try (
            InputStream input = context.getContentResolver().openInputStream(uri);
            ByteArrayOutputStream output = new ByteArrayOutputStream()
        ) {
            if (input == null) throw new IOException("Codex login document is unavailable");
            byte[] buffer = new byte[16 * 1024];
            int count;
            int total = 0;
            while ((count = input.read(buffer)) >= 0) {
                if (count == 0) continue;
                total += count;
                if (total > MAX_AUTH_JSON_BYTES) throw new AuthJsonInvalidException();
                output.write(buffer, 0, count);
            }
            if (total == 0) throw new AuthJsonInvalidException();
            return output.toByteArray();
        }
    }

    private static boolean isCodexAuthJson(byte[] value)
        throws CharacterCodingException, JSONException {
        String text = StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(value))
            .toString();
        JSONObject root = new JSONObject(text);
        Object tokens = root.opt("tokens");
        Object apiKey = root.opt("OPENAI_API_KEY");
        return tokens instanceof JSONObject ||
            (apiKey instanceof String && !((String) apiKey).trim().isEmpty());
    }

    private static void installCodexAuth(byte[] value) throws IOException {
        File rootfs = new File(DEBIAN_ROOT);
        File codexHome = new File(rootfs, "root/.codex");
        if (Files.isSymbolicLink(codexHome.toPath())) {
            throw new IOException("Codex home is not a private directory");
        }
        if (!codexHome.isDirectory() && !codexHome.mkdirs()) {
            throw new IOException("Codex home is unavailable");
        }
        String rootPath = rootfs.getCanonicalPath() + File.separator;
        String homePath = codexHome.getCanonicalPath() + File.separator;
        if (!homePath.startsWith(rootPath)) {
            throw new IOException("Codex home escaped the private runtime");
        }
        setOwnerOnly(codexHome, true);

        File target = new File(codexHome, "auth.json");
        if (Files.isSymbolicLink(target.toPath())) {
            throw new IOException("Codex login target is not a regular file");
        }
        File temporary = new File(codexHome, ".auth.json.import");
        if (Files.isSymbolicLink(temporary.toPath())) {
            throw new IOException("Codex login staging target is unsafe");
        }
        try (FileOutputStream output = new FileOutputStream(temporary, false)) {
            output.write(value);
            output.write('\n');
            output.getFD().sync();
        }
        setOwnerOnly(temporary, false);
        try {
            Files.move(
                temporary.toPath(),
                target.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING
            );
        } catch (AtomicMoveNotSupportedException error) {
            Files.move(
                temporary.toPath(),
                target.toPath(),
                StandardCopyOption.REPLACE_EXISTING
            );
        }
    }

    private EmbeddedRuntimeStartResult startCodex(String tokenSha256) {
        if (!new File(ENTER_DEBIAN).canExecute()) return EmbeddedRuntimeStartResult.PREPARING;
        String normalizedTokenSha256 = tokenSha256.toLowerCase(Locale.US);
        synchronized (this) {
            try {
                if (!providerStore.configured()) return EmbeddedRuntimeStartResult.PREPARING;
                providerStore.stage(new File(DEBIAN_ROOT));
            } catch (Exception error) {
                return EmbeddedRuntimeStartResult.FAILED;
            }
            long now = SystemClock.elapsedRealtime();
            String installedTokenSha256;
            try {
                installedTokenSha256 = readStoredTokenSha256();
            } catch (IOException error) {
                installedTokenSha256 = null;
            }
            if (
                lastCodexStartRequestElapsedMs > 0 &&
                normalizedTokenSha256.equals(lastCodexStartTokenSha256) &&
                normalizedTokenSha256.equals(installedTokenSha256) &&
                now - lastCodexStartRequestElapsedMs < CODEX_START_DEDUP_WINDOW_MS
            ) {
                return EmbeddedRuntimeStartResult.REQUESTED;
            }
            lastCodexStartRequestElapsedMs = now;
            lastCodexStartTokenSha256 = normalizedTokenSha256;
        }
        EmbeddedRuntimeStartResult result = startFixedCommand(
            ENTER_DEBIAN,
            new String[] {
                START_CODEX,
                "--token-sha256", normalizedTokenSha256,
                "--foreground",
            },
            "ChebyCodex runtime"
        );
        if (result != EmbeddedRuntimeStartResult.REQUESTED) {
            synchronized (this) {
                lastCodexStartRequestElapsedMs = 0;
                lastCodexStartTokenSha256 = null;
            }
        }
        return result;
    }

    private static final class AuthJsonInvalidException extends Exception {}

    private EmbeddedRuntimeStartResult startFixedCommand(
        String executable,
        String[] arguments,
        String label
    ) {
        if (!new File(executable).canExecute()) return EmbeddedRuntimeStartResult.PREPARING;
        Intent intent = new Intent(ACTION_EXECUTE)
            .setClass(context, TermuxService.class)
            .setData(Uri.fromFile(new File(executable)))
            .putExtra(EXTRA_ARGUMENTS, arguments)
            .putExtra(EXTRA_WORKDIR, TERMUX_HOME)
            .putExtra(EXTRA_BACKGROUND, true)
            .putExtra(EXTRA_LABEL, label);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent);
            } else {
                context.startService(intent);
            }
            return EmbeddedRuntimeStartResult.REQUESTED;
        } catch (RuntimeException error) {
            return EmbeddedRuntimeStartResult.FAILED;
        }
    }
}
