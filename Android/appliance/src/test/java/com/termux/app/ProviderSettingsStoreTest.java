package com.termux.app;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class ProviderSettingsStoreTest {
    @Test public void blankTokenRetainsAndExplicitClearRemoves() throws Exception {
        JSONObject old = ProviderSettingsStore.defaults();
        old.getJSONObject("profiles").getJSONObject("glm").put("apiKey", "test-placeholder");
        JSONObject input = ProviderSettingsStore.defaults();
        assertEquals("test-placeholder", ProviderSettingsStore.merge(input, old).getJSONObject("profiles").getJSONObject("glm").getString("apiKey"));
        input.getJSONObject("profiles").getJSONObject("glm").put("clearApiKey", true);
        assertEquals("", ProviderSettingsStore.merge(input, old).getJSONObject("profiles").getJSONObject("glm").getString("apiKey"));
    }

    @Test public void remoteMcpRequiresExplicitServiceAuthorization() throws Exception {
        JSONObject input = ProviderSettingsStore.defaults();
        input.getJSONArray("mcpServers").put(server("notes", "https://example.test/mcp", false));
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.merge(input, ProviderSettingsStore.defaults()));
        input.getJSONArray("mcpServers").getJSONObject(0).put("remoteAuthorized", true);
        assertEquals(1, ProviderSettingsStore.merge(input, ProviderSettingsStore.defaults()).getJSONArray("mcpServers").length());
    }

    @Test public void changingMcpDestinationDoesNotCarrySavedCredential() throws Exception {
        JSONObject old = ProviderSettingsStore.defaults();
        old.getJSONArray("mcpServers").put(server("notes", "http://localhost:3111/mcp", false).put("token", "test-placeholder"));
        JSONObject input = ProviderSettingsStore.defaults();
        input.getJSONArray("mcpServers").put(server("notes", "http://localhost:3112/mcp", false));
        assertEquals("", ProviderSettingsStore.merge(input, old).getJSONArray("mcpServers").getJSONObject(0).getString("token"));
    }

    @Test public void changingModelDestinationDoesNotCarrySavedCredential() throws Exception {
        JSONObject old = ProviderSettingsStore.defaults();
        old.getJSONObject("profiles").getJSONObject("glm").put("apiKey", "test-placeholder");
        JSONObject input = ProviderSettingsStore.defaults();
        input.getJSONObject("profiles").getJSONObject("glm").put("baseUrl", "https://example.test/api");
        assertEquals("", ProviderSettingsStore.merge(input, old).getJSONObject("profiles").getJSONObject("glm").getString("apiKey"));
    }

    @Test public void openAiUsesSubscriptionLoginAndDropsLegacyApiCredential() throws Exception {
        JSONObject old = ProviderSettingsStore.defaults();
        old.getJSONObject("profiles").getJSONObject("openai")
            .put("baseUrl", "https://example.test/v1")
            .put("apiKey", "legacy-placeholder");
        assertTrue(ProviderSettingsStore.stripUnsupportedProviderState(old));
        assertEquals("", old.getJSONObject("profiles").getJSONObject("openai").getString("apiKey"));

        JSONObject incoming = ProviderSettingsStore.defaults().put("provider", "openai");
        incoming.getJSONObject("profiles").getJSONObject("openai")
            .put("baseUrl", "https://example.test/v1")
            .put("apiKey", "new-placeholder");
        JSONObject merged = ProviderSettingsStore.merge(incoming, old);
        JSONObject openai = merged.getJSONObject("profiles").getJSONObject("openai");
        assertEquals("https://api.openai.com/v1", openai.getString("baseUrl"));
        assertEquals("", openai.getString("apiKey"));
    }

    @Test public void duplicateNamesAndUrlCredentialsAreRejected() throws Exception {
        JSONObject input = ProviderSettingsStore.defaults();
        input.getJSONArray("mcpServers").put(server("notes", "http://localhost:3111/mcp", false));
        input.getJSONArray("mcpServers").put(server("notes", "http://localhost:3112/mcp", false));
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.merge(input, ProviderSettingsStore.defaults()));
        input.put("mcpServers", new JSONArray());
        input.getJSONObject("profiles").getJSONObject("glm").put("baseUrl", "https://user:password@example.test");
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.merge(input, ProviderSettingsStore.defaults()));
    }

    @Test public void legacyIndependentVisionStateIsRemoved() throws Exception {
        JSONObject legacy = ProviderSettingsStore.defaults()
            .put("visionEnabled", true)
            .put("visionBaseUrl", "https://api.minimax.io/v1")
            .put("visionKey", "test-placeholder")
            .put("clearVisionKey", false);
        assertTrue(ProviderSettingsStore.stripUnsupportedProviderState(legacy));
        for (String field : new String[]{"visionEnabled", "visionBaseUrl", "visionKey", "clearVisionKey"})
            assertFalse(legacy.has(field));
        assertFalse(ProviderSettingsStore.stripUnsupportedProviderState(legacy));
    }

    @Test public void supportedModelsAndPerModelEffortsAreValidated() throws Exception {
        for (String provider : new String[]{"glm", "openai"}) {
            JSONObject input = ProviderSettingsStore.defaults().put("provider", provider);
            assertEquals(provider, ProviderSettingsStore.merge(input, ProviderSettingsStore.defaults()).getString("provider"));
        }
        JSONObject invalidProvider = ProviderSettingsStore.defaults().put("provider", "deepseek");
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.merge(invalidProvider, ProviderSettingsStore.defaults()));
        JSONObject retiredProvider = ProviderSettingsStore.defaults().put("provider", "minimax");
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.merge(retiredProvider, ProviderSettingsStore.defaults()));
        JSONObject invalidGlm = ProviderSettingsStore.defaults();
        invalidGlm.getJSONObject("profiles").getJSONObject("glm").put("reasoningEffort", "xhigh");
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.merge(invalidGlm, ProviderSettingsStore.defaults()));
    }

    @Test public void retiredProvidersAndCredentialsMigrateToTwoMultimodalModels() throws Exception {
        JSONObject legacy = new JSONObject().put("provider", "minimax")
            .put("profiles", new JSONObject()
                .put("glm", new JSONObject().put("baseUrl", "https://api.z.ai/api/paas/v4")
                    .put("reasoningEffort", "low").put("apiKey", "test-placeholder"))
                .put("minimax", new JSONObject().put("baseUrl", "https://api.minimaxi.com/v1")
                    .put("reasoningEffort", "medium").put("apiKey", "retired-minimax-placeholder"))
                .put("deepseek", new JSONObject().put("baseUrl", "https://api.deepseek.com")
                    .put("reasoningEffort", "high").put("apiKey", "retired-placeholder")))
            .put("mcpServers", new JSONArray());
        assertTrue(ProviderSettingsStore.stripUnsupportedProviderState(legacy));
        assertEquals("glm", legacy.getString("provider"));
        JSONObject profiles = legacy.getJSONObject("profiles");
        assertTrue(profiles.has("glm"));
        assertFalse(profiles.has("minimax"));
        assertTrue(profiles.has("openai"));
        assertFalse(profiles.has("deepseek"));
        assertEquals("test-placeholder", profiles.getJSONObject("glm").getString("apiKey"));
    }

    @Test public void retainedCredentialsCannotPushMergedConfigurationPastReadLimit() throws Exception {
        JSONObject old = ProviderSettingsStore.defaults();
        old.getJSONObject("profiles").getJSONObject("glm").put("apiKey", "g".repeat(8192));
        JSONObject incoming = ProviderSettingsStore.defaults();
        for (int i = 0; i < 6; i++) {
            old.getJSONArray("mcpServers").put(server("service" + i, "http://localhost:" + (8000 + i), false)
                .put("token", "t".repeat(8192)));
            incoming.getJSONArray("mcpServers").put(server("service" + i, "http://localhost:" + (8000 + i), false));
        }
        incoming.getJSONArray("mcpServers").put(server("service6", "http://localhost:8006", false)
            .put("token", "n".repeat(8192)));
        assertTrue(ProviderSettingsStore.boundedPlaintext(old).length <= 65536);
        assertTrue(incoming.toString().getBytes(StandardCharsets.UTF_8).length < 65536);
        JSONObject merged = ProviderSettingsStore.merge(incoming, old);
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.boundedPlaintext(merged));
        assertEquals(8192, old.getJSONArray("mcpServers").getJSONObject(0).getString("token").length());
    }

    @Test public void limitCountsUtf8BytesAndAllowsExactBoundary() throws Exception {
        int overhead = new JSONObject().put("data", "").toString().getBytes(StandardCharsets.UTF_8).length;
        assertEquals(65536, ProviderSettingsStore.boundedPlaintext(
            new JSONObject().put("data", "a".repeat(65536 - overhead))).length);
        JSONObject multibyte = new JSONObject().put("data", "界".repeat(22000));
        assertTrue(multibyte.toString().length() < 65536);
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.boundedPlaintext(multibyte));
    }

    @Test public void corruptOrInvalidatedStoreIsRebuiltOnlyAfterCacheTombstone() throws Exception {
        for (Exception readFailure : new Exception[] {
            new IllegalArgumentException("corrupt ciphertext"), new java.security.UnrecoverableKeyException("invalidated")
        }) {
            List<String> actions = new ArrayList<>();
            ProviderSettingsStore.rebuildUnreadable(new ProviderSettingsStore.RecoveryActions() {
                public void verifyReadable() throws Exception { actions.add("read"); throw readFailure; }
                public void stageEmptyConfiguration() { actions.add("tombstone"); }
                public void replaceKeyAndSaveDefaults() throws Exception {
                    actions.add("new-key-and-defaults");
                    JSONObject clean = ProviderSettingsStore.defaults();
                    assertFalse(clean.has("visionKey"));
                    assertEquals(0, clean.getJSONArray("mcpServers").length());
                    assertEquals("", clean.getJSONObject("profiles").getJSONObject("glm").getString("apiKey"));
                    assertFalse(clean.getJSONObject("profiles").has("minimax"));
                    assertEquals("", clean.getJSONObject("profiles").getJSONObject("openai").getString("apiKey"));
                }
            });
            assertEquals(List.of("read", "tombstone", "new-key-and-defaults"), actions);
        }
    }

    @Test public void readableSettingsAreNeverDestroyedByStaleRecoveryConfirmation() {
        List<String> actions = new ArrayList<>();
        assertThrows(IllegalArgumentException.class, () -> ProviderSettingsStore.rebuildUnreadable(
            new ProviderSettingsStore.RecoveryActions() {
                public void verifyReadable() { actions.add("read"); }
                public void stageEmptyConfiguration() { actions.add("tombstone"); }
                public void replaceKeyAndSaveDefaults() { actions.add("new-key"); }
            }));
        assertEquals(List.of("read"), actions);
    }

    @Test public void failedTombstoneLeavesTheExistingKeyUntouched() {
        List<String> actions = new ArrayList<>();
        assertThrows(java.io.IOException.class, () -> ProviderSettingsStore.rebuildUnreadable(
            new ProviderSettingsStore.RecoveryActions() {
                public void verifyReadable() { throw new IllegalArgumentException("corrupt"); }
                public void stageEmptyConfiguration() throws Exception { actions.add("tombstone"); throw new java.io.IOException(); }
                public void replaceKeyAndSaveDefaults() { actions.add("new-key"); }
            }));
        assertEquals(List.of("tombstone"), actions);
    }

    private static JSONObject server(String name, String url, boolean remote) throws Exception {
        return new JSONObject().put("name", name).put("url", url).put("remoteAuthorized", remote).put("token", "");
    }
}
