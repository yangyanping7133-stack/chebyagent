package com.cheby.codex.mobile.ui

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.view.WindowManager
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.password
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.window.SecureFlagPolicy
import com.cheby.codex.runtime.EmbeddedRuntimeRegistry
import com.cheby.codex.mobile.gateway.OpenAiAccountState
import com.cheby.codex.mobile.gateway.OpenAiDeviceLogin
import java.net.URI
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

/** Secrets exist only in the dialog's unsaved memory and the runtime's secure save boundary. */
@Composable
internal fun ProviderSettingsDialog(
    onDismiss: () -> Unit,
    onSaved: () -> Unit,
    onRebuild: suspend () -> String?,
    onReadOpenAiAccount: suspend (Boolean) -> OpenAiAccountState,
    onStartOpenAiDeviceLogin: suspend () -> OpenAiDeviceLogin,
    onCancelOpenAiDeviceLogin: suspend (String) -> Unit,
    onLogoutOpenAiAccount: suspend () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var draft by remember { mutableStateOf<ProviderSettingsDraft?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var saving by remember { mutableStateOf(false) }
    var readFailed by remember { mutableStateOf(false) }
    var confirmRebuild by remember { mutableStateOf(false) }
    var recoveryNotice by remember { mutableStateOf<String?>(null) }

    // Cover the Activity while editing as well as the dialog's own secure window. Restore
    // only the secure bit we changed, preserving every other window flag.
    DisposableEffect(context) {
        val window = context.activity()?.window
        val wasSecure = window?.attributes?.flags?.and(WindowManager.LayoutParams.FLAG_SECURE) ==
            WindowManager.LayoutParams.FLAG_SECURE
        window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        onDispose {
            if (!wasSecure) window?.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
        }
    }
    LaunchedEffect(Unit) {
        try {
            draft = withContext(Dispatchers.IO) {
                ProviderSettingsDraft.read(EmbeddedRuntimeRegistry.providerSettings())
            }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            readFailed = true
            error = "无法读取安全配置。可先关闭后重试；若配置已损坏，可重新建立安全配置。"
        }
    }
    if (confirmRebuild) {
        AlertDialog(
            onDismissRequest = { confirmRebuild = false },
            title = { Text("重新建立安全配置？") },
            text = {
                Text("这会清除已存的模型配置及凭证，需要重新填写。会话和任务数据保留；有任务正在运行时不会执行。")
            },
            properties = DialogProperties(securePolicy = SecureFlagPolicy.SecureOn),
            dismissButton = {
                TextButton(onClick = { confirmRebuild = false }) { Text("取消") }
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmRebuild = false
                    saving = true
                    scope.launch {
                        try {
                            val failure = onRebuild()
                            if (failure == null) {
                                draft = withContext(Dispatchers.IO) {
                                    ProviderSettingsDraft.read(EmbeddedRuntimeRegistry.providerSettings())
                                }
                                readFailed = false
                                error = null
                                recoveryNotice = "已重新建立安全配置，请重新填写凭证。会话和任务数据已保留；尚未验证服务连接。"
                            } else error = failure
                        } catch (cancelled: CancellationException) {
                            throw cancelled
                        } catch (_: Exception) {
                            error = "安全配置重建未完成，请重试。任务数据未被清除。"
                        } finally { saving = false }
                    }
                }) { Text("清除配置并重新建立") }
            },
        )
    }
    Dialog(
        onDismissRequest = { if (!saving) onDismiss() },
        properties = DialogProperties(
            securePolicy = SecureFlagPolicy.SecureOn,
            usePlatformDefaultWidth = false,
        ),
    ) {
        Surface(
            modifier = Modifier.fillMaxWidth().fillMaxHeight(0.94f).padding(horizontal = 12.dp),
            shape = MaterialTheme.shapes.extraLarge,
        ) {
            Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("服务与登录", style = MaterialTheme.typography.titleLarge)
                val current = draft
                Column(
                    Modifier.weight(1f).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    if (current == null && error == null) CircularProgressIndicator()
                    recoveryNotice?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
                    if (current != null) {
                        Text("模型服务", style = MaterialTheme.typography.titleMedium)
                        Text("三个入口都支持文字、工具调用和图片理解。", style = MaterialTheme.typography.bodySmall)
                        PROVIDER_SPECS.forEach { (id, spec) ->
                            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                RadioButton(
                                    selected = current.provider == id,
                                    enabled = !saving,
                                    onClick = { draft = current.copy(provider = id) },
                                )
                                Column(Modifier.weight(1f)) {
                                    Text(spec.label, style = MaterialTheme.typography.bodyLarge)
                                    Text(spec.note, style = MaterialTheme.typography.bodySmall)
                                }
                            }
                        }
                        val selectedProvider = current.provider.takeIf { it in PROVIDER_SPECS } ?: "glm"
                        val spec = PROVIDER_SPECS.getValue(selectedProvider)
                        val profile = current.profiles.getValue(selectedProvider)
                        fun updateProfile(value: ProviderProfileDraft) {
                            draft = current.copy(profiles = current.profiles + (selectedProvider to value))
                        }
                        if (selectedProvider == "openai") {
                            ChatGptLoginSettings(
                                enabled = !saving,
                                onReadAccount = onReadOpenAiAccount,
                                onStartLogin = onStartOpenAiDeviceLogin,
                                onCancelLogin = onCancelOpenAiDeviceLogin,
                                onLogout = onLogoutOpenAiAccount,
                            )
                        } else {
                            SettingsTextField(
                                "服务地址", profile.baseUrl, !saving,
                                { updateProfile(profile.copy(baseUrl = it)) },
                            )
                            SecretSettingsField(
                                label = "${spec.label} API Token",
                                value = profile.apiKey,
                                configured = profile.apiKeyConfigured,
                                clear = profile.clearApiKey,
                                enabled = !saving,
                                onValue = { updateProfile(profile.copy(apiKey = it)) },
                                onClear = { updateProfile(profile.copy(clearApiKey = it, apiKey = "")) },
                            )
                        }
                        Text("模型和推理强度在每个会话中单独选择，档位直接来自 Codex 官方模型列表。保存后将重连服务；正在进行的任务不受影响。",
                            style = MaterialTheme.typography.bodySmall)
                    }
                    error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                    if (readFailed) {
                        TextButton(enabled = !saving, onClick = { confirmRebuild = true }) {
                            Text("重新建立安全配置")
                        }
                    }
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(enabled = !saving, onClick = onDismiss) { Text("取消") }
                    Button(
                        enabled = current != null && !saving,
                        onClick = {
                            val toSave = draft ?: return@Button
                            saving = true
                            error = null
                            scope.launch {
                                try {
                                    val failure = withContext(Dispatchers.IO) {
                                        EmbeddedRuntimeRegistry.saveProviderSettings(toSave.encode())
                                    }
                                    if (failure == null) {
                                        draft = null
                                        onSaved()
                                    } else error = failure
                                } catch (cancelled: CancellationException) {
                                    throw cancelled
                                } catch (_: Exception) {
                                    error = "设置保存失败，请重试。"
                                } finally {
                                    saving = false
                                }
                            }
                        },
                    ) { Text(if (saving) "正在保存…" else "保存") }
                }
            }
        }
    }
}

@Composable
private fun SettingsTextField(label: String, value: String, enabled: Boolean, onValue: (String) -> Unit) {
    OutlinedTextField(
        value = value, onValueChange = onValue, enabled = enabled,
        label = { Text(label) }, singleLine = true, modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun SecretSettingsField(
    label: String, value: String, configured: Boolean, clear: Boolean, enabled: Boolean,
    onValue: (String) -> Unit, onClear: (Boolean) -> Unit,
) {
    OutlinedTextField(
        value = value, onValueChange = onValue, enabled = enabled && !clear,
        label = { Text(label) }, singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        modifier = Modifier.fillMaxWidth().semantics { password() },
        supportingText = {
            Text(when {
                clear -> "保存后清除已存凭证"
                configured -> "已配置；留空保留，输入新值则替换"
                else -> "尚未配置"
            })
        },
    )
    if (configured) SettingsCheckbox("清除已存凭证", clear, enabled, onClear)
}

@Composable
private fun ChatGptLoginSettings(
    enabled: Boolean,
    onReadAccount: suspend (Boolean) -> OpenAiAccountState,
    onStartLogin: suspend () -> OpenAiDeviceLogin,
    onCancelLogin: suspend (String) -> Unit,
    onLogout: suspend () -> Unit,
) {
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    var account by remember { mutableStateOf<OpenAiAccountState?>(null) }
    var login by remember { mutableStateOf<OpenAiDeviceLogin?>(null) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirmLogout by remember { mutableStateOf(false) }

    suspend fun refresh(refreshToken: Boolean) {
        try {
            val current = onReadAccount(refreshToken)
            account = current
            if (current.signedIn) login = null
            error = null
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: Exception) {
            error = "暂时无法读取 ChatGPT 登录状态。"
        }
    }

    LaunchedEffect(Unit) {
        busy = true
        refresh(false)
        busy = false
    }
    LaunchedEffect(login?.loginId) {
        while (login != null) {
            delay(2_500)
            refresh(false)
        }
    }
    if (confirmLogout) {
        AlertDialog(
            onDismissRequest = { confirmLogout = false },
            title = { Text("退出 ChatGPT？") },
            text = { Text("这台手机会删除 Codex 保存的 ChatGPT 登录状态。") },
            dismissButton = {
                TextButton(onClick = { confirmLogout = false }) { Text("取消") }
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmLogout = false
                    busy = true
                    scope.launch {
                        try {
                            onLogout()
                            account = OpenAiAccountState(false)
                            login = null
                            error = null
                        } catch (cancelled: CancellationException) {
                            throw cancelled
                        } catch (_: Exception) {
                            error = "退出未完成，请重试。"
                        } finally {
                            busy = false
                        }
                    }
                }) { Text("退出登录") }
            },
        )
    }

    Text("ChatGPT 套餐登录", style = MaterialTheme.typography.titleMedium)
    when {
        account?.signedIn == true -> {
            val plan = account?.planType?.takeIf(String::isNotBlank)?.uppercase()
            Text(if (plan == null) "已登录 ChatGPT" else "已登录 ChatGPT · $plan")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(enabled = enabled && !busy, onClick = {
                    busy = true
                    scope.launch {
                        try { refresh(true) } finally { busy = false }
                    }
                }) { Text("刷新状态") }
                TextButton(enabled = enabled && !busy, onClick = { confirmLogout = true }) {
                    Text("退出登录")
                }
            }
        }
        login != null -> {
            val current = requireNotNull(login)
            Text("请在任意已登录 ChatGPT 的设备上打开：", style = MaterialTheme.typography.bodySmall)
            SelectionContainer { Text(current.verificationUrl) }
            Text("一次性登录码", style = MaterialTheme.typography.labelLarge)
            SelectionContainer {
                Text(current.userCode, style = MaterialTheme.typography.headlineSmall)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(enabled = enabled && !busy, onClick = {
                    clipboard.setText(AnnotatedString("${current.verificationUrl}\n${current.userCode}"))
                }) { Text("复制地址和代码") }
                TextButton(enabled = enabled && !busy, onClick = {
                    busy = true
                    scope.launch {
                        try {
                            onCancelLogin(current.loginId)
                            login = null
                            error = null
                        } catch (cancelled: CancellationException) {
                            throw cancelled
                        } catch (_: Exception) {
                            error = "取消登录未完成，请重试。"
                        } finally {
                            busy = false
                        }
                    }
                }) { Text("取消登录") }
            }
            Text("本机不会自动打开浏览器；完成后此页会自动更新。", style = MaterialTheme.typography.bodySmall)
        }
        else -> {
            Text("未登录。这里使用 ChatGPT/Codex 套餐，不需要填 API Token。", style = MaterialTheme.typography.bodySmall)
            Button(enabled = enabled && !busy, onClick = {
                busy = true
                scope.launch {
                    try {
                        login = onStartLogin()
                        error = null
                    } catch (cancelled: CancellationException) {
                        throw cancelled
                    } catch (_: Exception) {
                        error = "无法生成登录码，请确认本机服务已连接后重试。"
                    } finally {
                        busy = false
                    }
                }
            }) { Text("生成登录码") }
        }
    }
    if (busy) CircularProgressIndicator(Modifier.size(24.dp))
    error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
}

@Composable
private fun SettingsCheckbox(label: String, checked: Boolean, enabled: Boolean, onChecked: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = checked, onCheckedChange = onChecked, enabled = enabled)
        Text(label, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
    }
}

private fun Context.activity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.takeIf { it !== this }?.activity()
    else -> null
}

private fun isLoopbackService(url: String): Boolean = runCatching {
    URI(url.trim()).host?.lowercase() in setOf("localhost", "127.0.0.1")
}.getOrDefault(false)

private data class ProviderProfileDraft(
    val baseUrl: String,
    val reasoningEffort: String,
    val apiKeyConfigured: Boolean = false,
    val apiKey: String = "",
    val clearApiKey: Boolean = false,
)

private data class ProviderUiSpec(
    val label: String,
    val baseUrl: String,
    val defaultEffort: String,
    val efforts: List<Pair<String, String>>,
    val note: String,
)

private data class McpServerDraft(
    val uiId: String = UUID.randomUUID().toString(),
    val name: String,
    val url: String = "http://127.0.0.1:8080/mcp",
    val enabled: Boolean = true,
    val remoteAuthorized: Boolean = false,
    val tokenConfigured: Boolean = false,
    val token: String = "",
    val clearToken: Boolean = false,
)

private data class ProviderSettingsDraft(
    val provider: String,
    val profiles: Map<String, ProviderProfileDraft>,
    val mcpServers: List<McpServerDraft>,
) {
    fun encode(): String = buildJsonObject {
        put("provider", provider)
        put("profiles", buildJsonObject {
            profiles.forEach { (name, profile) ->
                put(name, buildJsonObject {
                    put("baseUrl", profile.baseUrl.trim())
                    put("reasoningEffort", profile.reasoningEffort)
                    put("apiKey", profile.apiKey)
                    put("clearApiKey", profile.clearApiKey)
                })
            }
        })
        put("mcpServers", buildJsonArray {
            mcpServers.forEach { server ->
                add(buildJsonObject {
                    put("name", server.name.trim())
                    put("url", server.url.trim())
                    put("enabled", server.enabled)
                    put("remoteAuthorized", server.remoteAuthorized && !isLoopbackService(server.url))
                    put("token", server.token)
                    put("clearToken", server.clearToken)
                })
            }
        })
    }.toString()

    companion object {
        fun read(encoded: String): ProviderSettingsDraft {
            val root = Json.parseToJsonElement(encoded).jsonObject
            require(!root.containsKey("error")) { "Secure settings unavailable" }
            val profileObjects = root["profiles"] as? JsonObject
            val profiles = PROVIDER_SPECS.keys.associateWith { name ->
                val spec = PROVIDER_SPECS.getValue(name)
                val value = profileObjects?.get(name) as? JsonObject ?: JsonObject(emptyMap())
                ProviderProfileDraft(
                    baseUrl = value.text("baseUrl") ?: spec.baseUrl,
                    reasoningEffort = value.text("reasoningEffort")?.takeIf { effort ->
                        spec.efforts.any { it.first == effort }
                    } ?: spec.defaultEffort,
                    apiKeyConfigured = value.flag("apiKeyConfigured"),
                )
            }
            return ProviderSettingsDraft(
                provider = root.text("provider")?.takeIf { it in PROVIDER_SPECS } ?: "openai",
                profiles = profiles,
                mcpServers = (root["mcpServers"] as? JsonArray).orEmpty().map { element ->
                    val value = element.jsonObject
                    McpServerDraft(
                        name = requireNotNull(value.text("name")),
                        url = requireNotNull(value.text("url")),
                        enabled = value.flag("enabled"),
                        remoteAuthorized = value.flag("remoteAuthorized"),
                        tokenConfigured = value.flag("tokenConfigured"),
                    )
                },
            )
        }
    }
}

private fun JsonObject.text(name: String): String? = (get(name) as? JsonPrimitive)?.contentOrNull
private fun JsonObject.flag(name: String): Boolean = (get(name) as? JsonPrimitive)?.booleanOrNull == true
private val PROVIDER_SPECS = linkedMapOf(
    "glm" to ProviderUiSpec(
        label = "GLM 5.3 Flash",
        baseUrl = "https://api.z.ai/api/paas/v4",
        defaultEffort = "low",
        efforts = listOf("low" to "低", "high" to "高", "max" to "最高"),
        note = "默认主流程，响应快。",
    ),
    "minimax" to ProviderUiSpec(
        label = "MiniMax M3",
        baseUrl = "https://api.minimaxi.com/v1",
        defaultEffort = "medium",
        efforts = listOf(
            "low" to "低", "medium" to "中", "high" to "高",
            "xhigh" to "更高", "max" to "最高",
        ),
        note = "中国区 Coding Plan，低强度关闭思考，其余为自适应思考。",
    ),
    "openai" to ProviderUiSpec(
        label = "GPT-5.6 Sol",
        baseUrl = "https://api.openai.com/v1",
        defaultEffort = "high",
        efforts = listOf(
            "none" to "关闭", "low" to "低", "medium" to "中",
            "high" to "高", "xhigh" to "更高", "max" to "最高",
        ),
        note = "使用 ChatGPT/Codex 套餐登录。新会话默认 Sol 高强度。",
    ),
)
