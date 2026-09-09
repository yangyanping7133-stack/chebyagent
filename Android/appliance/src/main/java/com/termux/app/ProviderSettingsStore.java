package com.termux.app;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.AtomicFile;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.KeyStore;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Keystore-encrypted, backup-excluded user configuration; never logged. */
final class ProviderSettingsStore {
    private static final String ALIAS = "cheby_provider_settings_v1";
    private static final int LIMIT = 65536;
    private final AtomicFile file;

    ProviderSettingsStore(Context context) {
        file = new AtomicFile(new File(context.getNoBackupFilesDir(), "providers.enc"));
    }

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (store.containsAlias(ALIAS)) return (SecretKey) store.getKey(ALIAS, null);
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
            KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setKeySize(256).build());
        return generator.generateKey();
    }

    synchronized JSONObject load() throws Exception {
        if (!file.getBaseFile().exists()) return defaults();
        if (Files.isSymbolicLink(file.getBaseFile().toPath()) || file.getBaseFile().length() > LIMIT + 128)
            throw new IllegalArgumentException("配置存储不可用。");
        byte[] stored = file.readFully();
        if (stored.length < 29 || stored[0] != 1) throw new IllegalArgumentException("配置存储不可用。");
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Arrays.copyOfRange(stored, 1, 13)));
        byte[] plain = cipher.doFinal(stored, 13, stored.length - 13);
        try {
            JSONObject result = new JSONObject(new String(plain, StandardCharsets.UTF_8));
            if (stripUnsupportedProviderState(result)) write(result);
            return result;
        }
        finally { Arrays.fill(plain, (byte) 0); }
    }

    synchronized void save(String json) throws Exception {
        if (json == null || json.getBytes(StandardCharsets.UTF_8).length > LIMIT)
            throw new IllegalArgumentException("设置内容过长。");
        JSONObject merged = merge(new JSONObject(json), load());
        write(merged);
    }

    private void write(JSONObject settings) throws Exception {
        byte[] plain = boundedPlaintext(settings);
        try {
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] encrypted = cipher.doFinal(plain);
            FileOutputStream output = null;
            try {
                output = file.startWrite();
                output.write(1);
                output.write(cipher.getIV());
                output.write(encrypted);
                file.finishWrite(output);
            } catch (Exception error) {
                if (output != null) file.failWrite(output);
                throw error;
            }
        } finally { Arrays.fill(plain, (byte) 0); }
    }

    static byte[] boundedPlaintext(JSONObject settings) {
        byte[] plain = settings.toString().getBytes(StandardCharsets.UTF_8);
        if (plain.length > LIMIT) {
            Arrays.fill(plain, (byte) 0);
            throw new IllegalArgumentException("合并后的设置内容过长，请减少服务或凭证长度。");
        }
        return plain;
    }

    /** Called only after explicit UI confirmation and under the gateway's idle-change lock. */
    synchronized void rebuild(File rootfs) throws Exception {
        rebuildUnreadable(new RecoveryActions() {
            @Override public void verifyReadable() throws Exception { publicSettings(); }
            @Override public void stageEmptyConfiguration() throws Exception {
                // A nonexistent guest cannot own a running bridge or cached credentials.
                if (rootfs.exists()) stageSettings(rootfs, new JSONObject());
            }
            @Override public void replaceKeyAndSaveDefaults() throws Exception {
                KeyStore store = KeyStore.getInstance("AndroidKeyStore");
                store.load(null);
                if (store.containsAlias(ALIAS)) store.deleteEntry(ALIAS);
                // Do not load or merge the corrupt old file. Atomic write replaces only
                // providers.enc, leaving conversations and every other application file alone.
                write(defaults());
            }
        });
    }

    interface RecoveryActions {
        void verifyReadable() throws Exception;
        void stageEmptyConfiguration() throws Exception;
        void replaceKeyAndSaveDefaults() throws Exception;
    }

    static void rebuildUnreadable(RecoveryActions actions) throws Exception {
        boolean unreadable = false;
        try { actions.verifyReadable(); }
        catch (Exception error) { unreadable = true; }
        if (!unreadable) throw new IllegalArgumentException("安全配置可以正常读取，无需重新建立。");
        // Publish {} before deleting a key. The bridge must consume this tombstone instead
        // of treating a missing settings file as permission to reuse its in-memory cache.
        actions.stageEmptyConfiguration();
        actions.replaceKeyAndSaveDefaults();
    }

    synchronized String publicSettings() throws Exception {
        JSONObject result = load();
        JSONObject profiles = result.getJSONObject("profiles");
        for (String provider : new String[]{"glm", "minimax", "openai"}) redact(profiles.getJSONObject(provider), "apiKey");
        JSONArray servers = result.getJSONArray("mcpServers");
        for (int i = 0; i < servers.length(); i++) redact(servers.getJSONObject(i), "token");
        return result.toString();
    }

    synchronized boolean configured() throws Exception {
        JSONObject value = load();
        String provider = value.getString("provider");
        return "openai".equals(provider) ||
            !value.getJSONObject("profiles").getJSONObject(provider).optString("apiKey").isEmpty();
    }

    /** Only selected credentials enter the guest, in an owner-only one-shot handoff file. */
    synchronized void stage(File rootfs) throws Exception {
        JSONObject settings = load();
        String provider = settings.getString("provider");
        JSONObject profile = settings.getJSONObject("profiles").getJSONObject(provider);
        if (!"openai".equals(provider)) settings.put("apiKey", profile.getString("apiKey"));
        settings.put("baseUrl", profile.getString("baseUrl"));
        settings.put("reasoningEffort", profile.getString("reasoningEffort"));
        settings.remove("profiles");
        stageSettings(rootfs, settings);
    }

    private static void stageSettings(File rootfs, JSONObject settings) throws Exception {
        File directory = new File(rootfs, "root/.cheby");
        if (Files.isSymbolicLink(directory.toPath()) || !directory.getCanonicalPath().startsWith(rootfs.getCanonicalPath() + File.separator))
            throw new IllegalArgumentException("运行配置目录不可用。");
        if (!directory.isDirectory() && !directory.mkdirs()) throw new IllegalArgumentException("运行配置目录不可用。");
        File target = new File(directory, "provider-settings.json");
        if (Files.isSymbolicLink(target.toPath())) throw new IllegalArgumentException("运行配置文件不可用。");
        File temporary = File.createTempFile(".providers-", ".tmp", directory);
        byte[] plain = boundedPlaintext(settings);
        try {
            android.system.Os.chmod(temporary.getPath(), 0600);
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                output.write(plain);
                output.getFD().sync();
            }
            Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Arrays.fill(plain, (byte) 0);
            temporary.delete();
        }
    }

    static JSONObject defaults() throws Exception {
        return new JSONObject().put("provider", "openai")
            .put("profiles", new JSONObject()
                .put("glm", new JSONObject().put("baseUrl", "https://api.z.ai/api/paas/v4").put("reasoningEffort", "low").put("apiKey", ""))
                .put("minimax", new JSONObject().put("baseUrl", "https://api.minimaxi.com/v1").put("reasoningEffort", "medium").put("apiKey", ""))
                .put("openai", new JSONObject().put("baseUrl", "https://api.openai.com/v1").put("reasoningEffort", "high").put("apiKey", "")))
            .put("mcpServers", new JSONArray());
    }

    /** Migrate encrypted settings without exposing or dropping retained supported credentials. */
    static boolean stripUnsupportedProviderState(JSONObject settings) {
        boolean changed = false;
        for (String field : new String[]{"visionEnabled", "visionBaseUrl", "visionKey", "clearVisionKey"}) {
            if (settings.has(field)) {
                settings.remove(field);
                changed = true;
            }
        }
        try {
            JSONObject profiles = settings.optJSONObject("profiles");
            if (profiles == null) {
                profiles = defaults().getJSONObject("profiles");
                settings.put("profiles", profiles);
                changed = true;
            } else {
                JSONObject clean = defaults().getJSONObject("profiles");
                for (String id : new String[]{"glm", "minimax", "openai"}) {
                    if (!profiles.has(id)) {
                        profiles.put(id, clean.getJSONObject(id));
                        changed = true;
                    }
                }
                // OpenAI now uses Codex-managed ChatGPT OAuth. Retire any previously stored,
                // unused API key instead of silently retaining a credential with no UI owner.
                JSONObject openai = profiles.getJSONObject("openai");
                if (!openai.optString("apiKey").isEmpty()) {
                    openai.put("apiKey", "");
                    changed = true;
                }
                // This is the launch default for new Threads, not a global UI preference.
                // Existing Threads retain their own app-server-owned effort.
                if (!"high".equals(openai.optString("reasoningEffort"))) {
                    openai.put("reasoningEffort", "high");
                    changed = true;
                }
                if (profiles.has("deepseek")) {
                    profiles.remove("deepseek");
                    changed = true;
                }
            }
            if (!Set.of("glm", "minimax", "openai").contains(settings.optString("provider"))) {
                settings.put("provider", "glm");
                changed = true;
            }
            if (!settings.has("mcpServers")) {
                settings.put("mcpServers", new JSONArray());
                changed = true;
            }
        } catch (Exception error) {
            throw new IllegalArgumentException("配置存储不可用。", error);
        }
        return changed;
    }

    private static void redact(JSONObject value, String field) throws Exception {
        value.put(field + "Configured", !value.optString(field).isEmpty());
        value.remove(field);
    }

    private static String secret(JSONObject incoming, JSONObject old, String field, String clear) {
        if (incoming.optBoolean(clear)) return "";
        String value = incoming.optString(field, "").trim();
        if (value.isEmpty()) return old.optString(field, "");
        if (value.length() > 8192 || value.chars().anyMatch(c -> c < 33 || c > 126))
            throw new IllegalArgumentException("Token 格式无效。");
        return value;
    }

    private static String endpoint(String text, boolean allowRemote) throws Exception {
        if (text.length() > 4096) throw new IllegalArgumentException("服务地址过长。");
        URI uri = new URI(text);
        String host = uri.getHost();
        boolean local = "localhost".equalsIgnoreCase(host) || "127.0.0.1".equals(host) || "[::1]".equals(host) || "::1".equals(host);
        if (host == null || uri.getPort() == 0 || uri.getPort() > 65535 || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null ||
            (!"https".equals(uri.getScheme()) && !(local && "http".equals(uri.getScheme()))) || (!local && !allowRemote))
            throw new IllegalArgumentException("地址须为 HTTPS 或本机服务；远端 MCP 须逐项授权。");
        return text.replaceAll("/+$", "");
    }

    static JSONObject merge(JSONObject incoming, JSONObject old) throws Exception {
        JSONObject result = defaults();
        String provider = incoming.getString("provider");
        if (!Set.of("glm", "minimax", "openai").contains(provider))
            throw new IllegalArgumentException("请选择可用的多模态模型。");
        result.put("provider", provider);
        for (String id : new String[]{"glm", "minimax", "openai"}) {
            JSONObject source = incoming.getJSONObject("profiles").getJSONObject(id);
            JSONObject previous = old.getJSONObject("profiles").getJSONObject(id);
            String effort = source.getString("reasoningEffort");
            Set<String> efforts = "glm".equals(id)
                ? Set.of("low", "high", "max")
                : "minimax".equals(id)
                    ? Set.of("low", "medium", "high", "xhigh", "max")
                    : Set.of("none", "low", "medium", "high", "xhigh", "max");
            if (!efforts.contains(effort)) throw new IllegalArgumentException("推理强度无效。");
            String baseUrl = "openai".equals(id)
                ? "https://api.openai.com/v1"
                : endpoint(source.getString("baseUrl").trim(), true);
            if (!baseUrl.equals(previous.optString("baseUrl"))) previous = new JSONObject();
            String apiKey = "openai".equals(id)
                ? ""
                : secret(source, previous, "apiKey", "clearApiKey");
            result.getJSONObject("profiles").put(id, new JSONObject()
                .put("baseUrl", baseUrl)
                .put("reasoningEffort", effort)
                .put("apiKey", apiKey));
        }
        JSONArray servers = incoming.getJSONArray("mcpServers");
        if (servers.length() > 12) throw new IllegalArgumentException("最多配置 12 个 MCP 服务。");
        Set<String> names = new HashSet<>();
        for (int i = 0; i < servers.length(); i++) {
            JSONObject source = servers.getJSONObject(i);
            String name = source.getString("name").trim();
            String normalized = name.toLowerCase(Locale.ROOT);
            if (!name.matches("[A-Za-z0-9_-]{1,40}") || !names.add(normalized) ||
                Set.of("phonebridge", "cheby_vision", "vision", "codex", "local_mcp").contains(normalized) ||
                normalized.startsWith("cheby_") || normalized.startsWith("user_"))
                throw new IllegalArgumentException("MCP 名称须为不同的英文、数字、下划线或短横线。");
            boolean remote = source.optBoolean("remoteAuthorized");
            String url = endpoint(source.getString("url").trim(), remote);
            JSONObject previous = new JSONObject();
            JSONArray previousServers = old.getJSONArray("mcpServers");
            for (int j = 0; j < previousServers.length(); j++) {
                JSONObject candidate = previousServers.getJSONObject(j);
                // Never carry a token to a changed destination.
                if (name.equals(candidate.optString("name")) && url.equals(candidate.optString("url"))) previous = candidate;
            }
            result.getJSONArray("mcpServers").put(new JSONObject().put("name", name).put("url", url)
                .put("enabled", source.optBoolean("enabled", true)).put("remoteAuthorized", remote)
                .put("token", secret(source, previous, "token", "clearToken")));
        }
        return result;
    }
}
