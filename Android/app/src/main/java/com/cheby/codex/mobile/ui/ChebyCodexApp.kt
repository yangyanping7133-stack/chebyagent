package com.cheby.codex.mobile.ui

import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.provider.MediaStore
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.FileProvider
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Divider
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.key
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.password
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.cheby.codex.mobile.BuildConfig
import com.cheby.codex.mobile.model.CodexThread
import com.cheby.codex.mobile.model.GatewayConnectionState
import com.cheby.codex.mobile.model.ThreadKey
import com.cheby.codex.mobile.model.ThreadRunState
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MessagePresentation
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.orderedBlocks
import com.cheby.codex.mobile.model.readableText
import com.cheby.codex.mobile.data.OutboxEntry
import com.cheby.codex.mobile.data.OutboxKey
import com.cheby.codex.mobile.data.OutboxState
import com.cheby.codex.mobile.data.key
import com.cheby.codex.mobile.gateway.GatewayKind
import com.cheby.codex.mobile.gateway.AvailableModel
import com.cheby.codex.mobile.ui.rich.MessageRow
import com.cheby.codex.mobile.ui.theme.Canvas
import com.cheby.codex.mobile.ui.theme.CompleteGreen
import com.cheby.codex.mobile.ui.theme.Divider as ChebyDivider
import com.cheby.codex.mobile.ui.theme.Ink
import com.cheby.codex.mobile.ui.theme.Muted
import com.cheby.codex.mobile.ui.theme.Paper
import com.cheby.codex.mobile.ui.theme.RiskRed
import com.cheby.codex.mobile.ui.theme.SignalBlue
import com.cheby.codex.mobile.ui.theme.WarningAmber
import com.cheby.codex.runtime.EmbeddedRuntimeRegistry
import com.cheby.codex.runtime.EmbeddedRuntimeStatus
import java.io.File
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay

@Composable
fun ChebyCodexApp(viewModel: AppViewModel) {
    val uiState by viewModel.uiState.collectAsState()
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var localSetupMessage by remember { mutableStateOf<String?>(null) }
    var showProviderSettings by remember { mutableStateOf(false) }
    var showSessionModelSettings by remember { mutableStateOf(false) }
    var showImageSourceDialog by remember { mutableStateOf(false) }
    var pendingCameraUri by rememberSaveable { mutableStateOf<String?>(null) }
    val cameraAvailable = remember(context) {
        Intent(MediaStore.ACTION_IMAGE_CAPTURE).resolveActivity(context.packageManager) != null
    }
    var embeddedRuntimeStatus by remember {
        mutableStateOf(EmbeddedRuntimeRegistry.status())
    }
    LaunchedEffect(uiState.gatewayKind) {
        if (uiState.gatewayKind != GatewayKind.LOCAL) return@LaunchedEffect
        while (true) {
            embeddedRuntimeStatus = EmbeddedRuntimeRegistry.status()
            delay(1_000)
        }
    }
    val imagePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenMultipleDocuments(),
    ) { uris ->
        viewModel.completeImageSelection(uris.map { it.toString() })
    }
    val camera = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.TakePicture(),
    ) { captured ->
        val uri = pendingCameraUri
        pendingCameraUri = null
        viewModel.completeImageSelection(if (captured && uri != null) listOf(uri) else emptyList())
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ThreadDrawer(
                threads = uiState.threads,
                currentThreadKey = uiState.currentThreadKey,
                onSelect = { key ->
                    viewModel.selectThread(key)
                    scope.launch { drawerState.close() }
                },
                onCreate = {
                    viewModel.createThread()
                    scope.launch { drawerState.close() }
                },
                onRename = viewModel::renameThread,
                onArchive = viewModel::archiveThread,
                onDelete = viewModel::deleteThread,
            )
        },
    ) {
        ConversationScreen(
            uiState = uiState,
            threadUiStates = viewModel.threadUiStates,
            onOpenThreads = { scope.launch { drawerState.open() } },
            onCreateThread = viewModel::createThread,
            onSend = viewModel::send,
            onStop = viewModel::stopCurrentTurn,
            onRetry = viewModel::retryOutbox,
            onDiscardOutbox = viewModel::discardOutbox,
            onAction = viewModel::performAction,
            onPickImages = { showImageSourceDialog = true },
            onRemoveImage = viewModel::removeSelectedImage,
            onToggleConnection = viewModel::toggleDemoConnection,
            onOpenPairing = viewModel::openPairing,
            onCancelPairing = viewModel::cancelPairing,
            onPair = viewModel::enrollAssistant,
            onChangeServer = viewModel::changeAssistantServer,
            embeddedRuntimeStatus = embeddedRuntimeStatus,
            onOpenProviderSettings = { showProviderSettings = true },
            onOpenSessionModelSettings = { showSessionModelSettings = true },
            onApplyProviderSettings = {
                localSetupMessage = if (viewModel.applySavedConfiguration()) {
                    "正在重新连接以应用已保存的配置；尚未验证服务连接。"
                } else {
                    "当前任务仍在进行，配置暂未切换。任务结束后，请再点“应用已保存的模型配置”。"
                }
            },
            onOpenAccessibilitySettings = {
                context.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            },
        )
    }
    if (showProviderSettings) {
        ProviderSettingsDialog(
            onDismiss = { showProviderSettings = false },
            onRebuild = { viewModel.rebuildProviderSettings() },
            onReadOpenAiAccount = viewModel::readOpenAiAccount,
            onStartOpenAiDeviceLogin = viewModel::startOpenAiDeviceLogin,
            onCancelOpenAiDeviceLogin = viewModel::cancelOpenAiDeviceLogin,
            onLogoutOpenAiAccount = viewModel::logoutOpenAiAccount,
            onSaved = {
                showProviderSettings = false
                localSetupMessage = if (viewModel.applySavedConfiguration()) {
                    "已保存，正在重新连接以应用新配置；尚未验证服务连接。"
                } else {
                    "已保存，当前任务继续使用原配置。任务结束后，可在更多菜单应用已保存的配置，或重新打开应用；尚未验证服务连接。"
                }
            },
        )
    }
    if (showSessionModelSettings) {
        SessionModelDialog(
            models = uiState.availableModels,
            currentModel = uiState.currentThread?.model,
            currentEffort = uiState.currentThread?.reasoningEffort,
            onDismiss = { showSessionModelSettings = false },
            onSave = { model, effort ->
                if (viewModel.selectCurrentThreadModel(model, effort)) {
                    showSessionModelSettings = false
                }
            },
        )
    }
    localSetupMessage?.let { message ->
        AlertDialog(
            onDismissRequest = { localSetupMessage = null },
            confirmButton = {
                TextButton(onClick = { localSetupMessage = null }) { Text("知道了") }
            },
            title = { Text("本机设置") },
            text = { Text(message) },
        )
    }
    if (showImageSourceDialog) {
        AlertDialog(
            onDismissRequest = { showImageSourceDialog = false },
            title = { Text("添加图片") },
            text = {
                Text(
                    if (cameraAvailable) {
                        "可以直接拍摄，也可以选择手机里已有的图片。"
                    } else {
                        "当前手机没有可调用的相机应用，请从相册选择已有照片。"
                    },
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        showImageSourceDialog = false
                        if (!cameraAvailable) {
                            if (viewModel.beginImageSelection()) {
                                imagePicker.launch(arrayOf("image/jpeg", "image/png"))
                            }
                        } else if (viewModel.beginImageSelection()) {
                            val uri = createCameraInputUri(context)
                            pendingCameraUri = uri.toString()
                            runCatching { camera.launch(uri) }.onFailure {
                                pendingCameraUri = null
                                viewModel.completeImageSelection(emptyList())
                            }
                        }
                    },
                ) { Text(if (cameraAvailable) "拍照" else "从相册选择") }
            },
            dismissButton = if (cameraAvailable) {
                {
                TextButton(
                    onClick = {
                        showImageSourceDialog = false
                        if (viewModel.beginImageSelection()) {
                            imagePicker.launch(arrayOf("image/jpeg", "image/png"))
                        }
                    },
                ) { Text("从相册选择") }
                }
            } else {
                null
            },
        )
    }
}

internal fun createCameraInputUri(context: android.content.Context): Uri {
    val directory = File(context.cacheDir, "camera-inputs")
    check(directory.isDirectory || directory.mkdirs())
    val file = File.createTempFile("capture-", ".jpg", directory)
    return FileProvider.getUriForFile(
        context,
        "${context.packageName}.fileprovider",
        file,
    )
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalComposeUiApi::class)
@Composable
private fun ConversationScreen(
    uiState: AppUiState,
    threadUiStates: ThreadUiStateRegistry,
    onOpenThreads: () -> Unit,
    onCreateThread: () -> Unit,
    onSend: (String) -> Boolean,
    onStop: () -> Unit,
    onRetry: (OutboxKey) -> Unit,
    onDiscardOutbox: (OutboxKey) -> Unit,
    onAction: (String) -> Unit,
    onPickImages: () -> Unit,
    onRemoveImage: (String) -> Unit,
    onToggleConnection: () -> Unit,
    onOpenPairing: () -> Unit,
    onCancelPairing: () -> Unit,
    onPair: (String, String, String) -> Unit,
    onChangeServer: (String) -> Unit,
    embeddedRuntimeStatus: EmbeddedRuntimeStatus,
    onOpenProviderSettings: () -> Unit,
    onOpenSessionModelSettings: () -> Unit,
    onApplyProviderSettings: () -> Unit,
    onOpenAccessibilitySettings: () -> Unit,
) {
    var menuExpanded by remember { mutableStateOf(false) }
    val authorizationContext = LocalContext.current
    val focusManager = LocalFocusManager.current
    val softwareKeyboardController = LocalSoftwareKeyboardController.current
    val activeThreadKey = uiState.currentThreadKey
    val activeThreadUiKey = activeThreadKey?.let {
        ThreadUiKey(uiState.currentSessionScope, uiState.currentSessionGeneration, it)
    }
    var draft by rememberSaveable(
        uiState.currentSessionScope,
        uiState.currentSessionGeneration,
        activeThreadKey?.value,
    ) {
        mutableStateOf(activeThreadUiKey?.let(threadUiStates::draft).orEmpty())
    }

    Scaffold(
        modifier = Modifier
            .fillMaxSize()
            .semantics { testTagsAsResourceId = true },
        containerColor = Canvas,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = Paper,
                    titleContentColor = Ink,
                ),
                navigationIcon = {
                    IconButton(onClick = onOpenThreads) {
                        Icon(Icons.Default.Menu, contentDescription = "打开会话列表")
                    }
                },
                title = {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable(onClick = onOpenThreads),
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("ChebyCodex", style = MaterialTheme.typography.titleMedium)
                            Spacer(Modifier.width(8.dp))
                            ConnectionBadge(
                                uiState.connectionState,
                                if (uiState.showPairing) GatewayKind.UNCONFIGURED else uiState.gatewayKind,
                                embeddedRuntimeStatus,
                            )
                        }
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                text = if (uiState.showPairing) "绑定助手" else uiState.currentThread?.title ?: "选择一个会话",
                                color = Muted,
                                style = MaterialTheme.typography.labelMedium,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                            Icon(
                                Icons.Default.ArrowDropDown,
                                contentDescription = null,
                                tint = Muted,
                                modifier = Modifier.size(18.dp),
                            )
                        }
                    }
                },
                actions = {
                    IconButton(
                        onClick = onCreateThread,
                        enabled = !uiState.showPairing && uiState.gatewayKind != GatewayKind.UNCONFIGURED,
                        modifier = Modifier.testTag("new_thread"),
                    ) {
                        Icon(Icons.Default.Add, contentDescription = "新建会话")
                    }
                    Box {
                        IconButton(onClick = { menuExpanded = true }) {
                            Icon(Icons.Default.MoreVert, contentDescription = "更多")
                        }
                        DropdownMenu(
                            expanded = menuExpanded &&
                                uiState.gatewayKind in setOf(
                                    GatewayKind.LOCAL,
                                    GatewayKind.DEMO,
                                    GatewayKind.RELAY,
                                ),
                            onDismissRequest = { menuExpanded = false },
                        ) {
                            if (uiState.gatewayKind == GatewayKind.LOCAL) {
                                DropdownMenuItem(
                                    text = { Text("当前会话的模型") },
                                    enabled = uiState.currentThreadKey != null && uiState.availableModels.isNotEmpty(),
                                    onClick = {
                                        menuExpanded = false
                                        onOpenSessionModelSettings()
                                    },
                                )
                                DropdownMenuItem(
                                    text = { Text("服务与登录") },
                                    onClick = {
                                        menuExpanded = false
                                        onOpenProviderSettings()
                                    },
                                )
                                DropdownMenuItem(
                                    text = { Text("应用已保存的模型配置") },
                                    onClick = {
                                        menuExpanded = false
                                        onApplyProviderSettings()
                                    },
                                )
                                DropdownMenuItem(
                                    text = { Text("打开 ChebyNode 无障碍授权") },
                                    onClick = {
                                        menuExpanded = false
                                        onOpenAccessibilitySettings()
                                    },
                                )
                                DropdownMenuItem(
                                    text = { Text("操作授权") },
                                    onClick = {
                                        menuExpanded = false
                                        val intent = Intent().setClassName(
                                            authorizationContext.packageName,
                                            "com.chebysight.chebyagent.android.CommerceAuthorizationActivity",
                                        )
                                        if (intent.resolveActivity(authorizationContext.packageManager) != null) {
                                            authorizationContext.startActivity(intent)
                                        }
                                    },
                                )
                            } else if (uiState.gatewayKind == GatewayKind.DEMO) {
                                DropdownMenuItem(
                                    text = {
                                        Text(
                                            if (uiState.connectionState == GatewayConnectionState.ONLINE) "模拟断网" else "恢复连接",
                                        )
                                    },
                                    onClick = {
                                        menuExpanded = false
                                        onToggleConnection()
                                    },
                                )
                            }
                            if (uiState.gatewayKind != GatewayKind.LOCAL) {
                                DropdownMenuItem(
                                    text = {
                                        Text(
                                            if (uiState.gatewayKind == GatewayKind.RELAY) {
                                                "更换服务器地址"
                                            } else {
                                                "绑定助手"
                                            },
                                        )
                                    },
                                    onClick = {
                                        menuExpanded = false
                                        onOpenPairing()
                                    },
                                )
                            }
                        }
                    }
                },
            )
        },
        bottomBar = {
            if (!uiState.showPairing) {
                Composer(
                    draft = draft,
                    enabled = uiState.canSend,
                    recovering = uiState.isConversationRecovering,
                    selectedImages = uiState.selectedImages,
                    localImageEnabled = uiState.localImageCapability != null,
                    maxImages = uiState.localImageCapability?.maxImagesPerTurn ?: 0,
                    preparing = uiState.preparingOutboxKey != null,
                    attachmentError = uiState.attachmentError,
                    canStop = uiState.canStopTurn,
                    stopping = uiState.stoppingTurn,
                    stopError = uiState.stopTurnError,
                    onStop = onStop,
                    onPickImages = onPickImages,
                    onRemoveImage = onRemoveImage,
                    onDraftChange = {
                        draft = it
                        activeThreadUiKey?.let { key -> threadUiStates.updateDraft(key, it) }
                    },
                    onSend = {
                        if ((draft.isNotBlank() || uiState.selectedImages.isNotEmpty()) && onSend(draft)) {
                            draft = ""
                            activeThreadUiKey?.let { key -> threadUiStates.updateDraft(key, "") }
                            focusManager.clearFocus(force = true)
                            softwareKeyboardController?.hide()
                        }
                    },
                )
            }
        },
    ) { padding ->
        if (uiState.showPairing) {
            PairingScreen(
                isPairing = uiState.isPairing,
                error = uiState.pairingError,
                canCancel = uiState.gatewayKind != GatewayKind.UNCONFIGURED,
                addressOnly = uiState.gatewayKind == GatewayKind.RELAY,
                onCancel = onCancelPairing,
                onPair = onPair,
                onChangeServer = onChangeServer,
                modifier = Modifier.padding(padding),
            )
        } else if (uiState.currentThreadKey == null) {
            EmptyConversation(
                onCreate = onCreateThread,
                modifier = Modifier.padding(padding),
            )
        } else if (uiState.isConversationRecovering || uiState.currentConversation == null) {
            RecoveringConversation(modifier = Modifier.padding(padding))
        } else {
            val threadKey = requireNotNull(activeThreadKey)
            val threadUiKey = requireNotNull(activeThreadUiKey)
            key(threadUiKey) {
                ThreadMessageList(
                    threadKey = threadKey,
                    threadUiKey = threadUiKey,
                    uiState = uiState,
                    registry = threadUiStates,
                    onAction = onAction,
                    onRetry = onRetry,
                    onDiscardOutbox = onDiscardOutbox,
                    modifier = Modifier.padding(padding),
                )
            }
        }
    }
}

@Composable
private fun SessionModelDialog(
    models: List<AvailableModel>,
    currentModel: String?,
    currentEffort: String?,
    onDismiss: () -> Unit,
    onSave: (String, String) -> Unit,
) {
    val initialModel = models.firstOrNull { it.id == currentModel }
        ?: models.firstOrNull(AvailableModel::isDefault)
        ?: models.firstOrNull()
    var selectedModelId by remember(currentModel, models) {
        mutableStateOf(initialModel?.id.orEmpty())
    }
    val selectedModel = models.firstOrNull { it.id == selectedModelId } ?: initialModel
    val supportedEfforts = selectedModel?.supportedReasoningEfforts.orEmpty()
    val initialEffort = currentEffort?.takeIf { effort ->
        selectedModel?.id == currentModel && supportedEfforts.any { it.value == effort }
    } ?: selectedModel?.defaultReasoningEffort.orEmpty()
    var selectedEffort by remember(selectedModelId, currentEffort) { mutableStateOf(initialEffort) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("当前会话的模型") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (models.isEmpty()) {
                    Text("官方模型列表暂未就绪。")
                } else {
                    Text("只影响当前会话的后续回合。", style = MaterialTheme.typography.bodySmall)
                    models.forEach { model ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            RadioButton(
                                selected = model.id == selectedModelId,
                                onClick = {
                                    selectedModelId = model.id
                                    selectedEffort = model.defaultReasoningEffort
                                },
                            )
                            Column {
                                Text(model.displayName)
                                if (model.description.isNotBlank()) {
                                    Text(model.description, style = MaterialTheme.typography.bodySmall, maxLines = 2)
                                }
                            }
                        }
                    }
                    Text("推理强度", style = MaterialTheme.typography.labelLarge)
                    supportedEfforts.forEach { effort ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            RadioButton(
                                selected = effort.value == selectedEffort,
                                onClick = { selectedEffort = effort.value },
                            )
                            Text(effortLabel(effort.value, effort.description))
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = selectedModel != null && selectedEffort.isNotBlank(),
                onClick = { selectedModel?.let { onSave(it.id, selectedEffort) } },
            ) { Text("应用到当前会话") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

private fun effortLabel(value: String, description: String): String = when (value) {
    "none" -> "关闭"
    "minimal" -> "最低"
    "low" -> "低"
    "medium" -> "中"
    "high" -> "高"
    "xhigh" -> "更高"
    "max" -> "最高"
    else -> description.ifBlank { value }
}

@Composable
private fun ThreadMessageList(
    threadKey: ThreadKey,
    threadUiKey: ThreadUiKey,
    uiState: AppUiState,
    registry: ThreadUiStateRegistry,
    onAction: (String) -> Unit,
    onRetry: (OutboxKey) -> Unit,
    onDiscardOutbox: (OutboxKey) -> Unit,
    modifier: Modifier = Modifier,
) {
    val saved = registry.scroll(threadUiKey)
    val listState = rememberLazyListState(saved.index, saved.offset)
    val pending = uiState.outbox.filter {
        it.sessionScope == uiState.currentSessionScope && it.threadKey == threadKey
    }
    val visibleMessages = visibleConversationMessages(uiState.messages, pending)
    val accepted = uiState.acceptedUserMessages.filter {
        it.sessionScope == uiState.currentSessionScope &&
            it.sessionGeneration == uiState.currentSessionGeneration &&
            it.threadKey == threadKey
    }
    val timeline = conversationTimeline(visibleMessages, accepted, pending)
    val latestCanonicalRevision = timeline.asReversed().firstNotNullOfOrNull { entry ->
        when (entry) {
            is ThreadTimelineEntry.Canonical -> entry.message.revision
            is ThreadTimelineEntry.Process -> entry.messages.maxOfOrNull(RichMessage::revision)
            else -> null
        }
    }
    var previousTimelineSize by remember(threadUiKey) { mutableStateOf(timeline.size) }
    var followingLatest by remember(threadUiKey) { mutableStateOf(false) }
    var programmaticScroll by remember(threadUiKey) { mutableStateOf(false) }
    DisposableEffect(threadUiKey, listState) {
        onDispose {
            registry.saveScroll(
                threadUiKey,
                listState.firstVisibleItemIndex,
                listState.firstVisibleItemScrollOffset,
            )
        }
    }
    LaunchedEffect(timeline.size, latestCanonicalRevision) {
        val lastVisibleItemIndex = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1
        val shouldFollowLatest = shouldFollowTimelineChange(
            previousTimelineSize = previousTimelineSize,
            currentTimelineSize = timeline.size,
            lastVisibleItemIndex = lastVisibleItemIndex,
            wasFollowingLatest = followingLatest,
        )
        previousTimelineSize = timeline.size
        if (shouldFollowLatest) {
            followingLatest = true
            programmaticScroll = true
            try {
                listState.animateScrollToItem(timeline.size)
            } finally {
                programmaticScroll = false
            }
        }
    }
    LaunchedEffect(threadUiKey, listState) {
        snapshotFlow { listState.isScrollInProgress }.collect { scrolling ->
            if (!scrolling && !programmaticScroll) {
                val lastVisibleItemIndex =
                    listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: -1
                followingLatest =
                    previousTimelineSize <= 0 || lastVisibleItemIndex >= previousTimelineSize
            }
        }
    }

    LazyColumn(
        state = listState,
        modifier = modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        item {
            Text(
                "今天",
                color = Muted,
                style = MaterialTheme.typography.labelMedium,
                modifier = Modifier.fillMaxWidth(),
            )
        }
        items(timeline, key = ThreadTimelineEntry::key) { entry ->
            when (entry) {
                is ThreadTimelineEntry.Canonical -> MessageRow(
                    message = entry.message,
                    onAction = onAction,
                    actionRetryNotBeforeMillis = uiState.actionRetryNotBeforeMillis,
                )
                is ThreadTimelineEntry.Process -> ProcessGroupRow(
                    entry = entry,
                    onAction = onAction,
                    actionRetryNotBeforeMillis = uiState.actionRetryNotBeforeMillis,
                )
                is ThreadTimelineEntry.Accepted -> AcceptedUserRow(entry.message)
                is ThreadTimelineEntry.Pending -> OutboxRow(
                    entry = entry.message,
                    local = uiState.gatewayKind == GatewayKind.LOCAL,
                    deliveryNotice = uiState.localDeliveryNotices[entry.message.key],
                    preparing = entry.message.key == uiState.preparingOutboxKey,
                    sending = entry.message.key in uiState.sendingOutboxKeys,
                    onRetry = onRetry,
                    onDiscard = onDiscardOutbox,
                )
            }
        }
        item { Spacer(Modifier.height(4.dp)) }
    }
}

@Composable
private fun ProcessGroupRow(
    entry: ThreadTimelineEntry.Process,
    onAction: (String) -> Unit,
    actionRetryNotBeforeMillis: Map<String, Long>,
) {
    var expanded by rememberSaveable(entry.key) { mutableStateOf(false) }
    val status = "${entry.messages.size} 项"
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(14.dp))
                .background(Paper)
                .border(1.dp, ChebyDivider, RoundedCornerShape(14.dp))
                .clickable { expanded = !expanded }
                .testTag("process-${entry.turnId}")
                .padding(horizontal = 14.dp, vertical = 11.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(28.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(Ink),
                contentAlignment = Alignment.Center,
            ) {
                Text("…", color = Paper, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.width(10.dp))
            Text(
                "过程",
                color = Ink,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.weight(1f))
            Text(status, color = Muted, style = MaterialTheme.typography.labelMedium)
            Spacer(Modifier.width(4.dp))
            Icon(
                Icons.Filled.ArrowDropDown,
                contentDescription = if (expanded) "收起过程" else "展开过程",
                tint = Muted,
                modifier = Modifier.rotate(if (expanded) 180f else 0f),
            )
        }
        if (expanded) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 14.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                entry.messages.forEach { message ->
                    MessageRow(
                        message = message,
                        onAction = onAction,
                        actionRetryNotBeforeMillis = actionRetryNotBeforeMillis,
                        modifier = Modifier.testTag("process-item-${message.id}"),
                    )
                }
            }
        }
    }
}

@Composable
private fun AcceptedUserRow(entry: AcceptedUserMessage) {
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Column(
            modifier = Modifier
                .fillMaxWidth(0.86f)
                .clip(RoundedCornerShape(18.dp))
                .background(SignalBlue)
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            entry.text.takeIf(String::isNotBlank)?.let {
                Text(it, color = Paper, style = MaterialTheme.typography.bodyLarge)
            }
            if (entry.imageCount > 0) {
                if (entry.text.isNotBlank()) Spacer(Modifier.height(6.dp))
                Text("[图片 × ${entry.imageCount}]", color = Paper, style = MaterialTheme.typography.bodyMedium)
            }
            Spacer(Modifier.height(6.dp))
            Text("已送达", color = Paper, style = MaterialTheme.typography.labelSmall)
        }
    }
}

@Composable
private fun OutboxRow(
    entry: OutboxEntry,
    local: Boolean,
    deliveryNotice: String?,
    preparing: Boolean,
    sending: Boolean,
    onRetry: (OutboxKey) -> Unit,
    onDiscard: (OutboxKey) -> Unit,
) {
    var now by remember(entry.retryNotBeforeMillis) { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(entry.retryNotBeforeMillis) {
        while (now < entry.retryNotBeforeMillis) {
            delay(minOf(1_000, entry.retryNotBeforeMillis - now))
            now = System.currentTimeMillis()
        }
    }
    val waiting = entry.retryNotBeforeMillis > now
    Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Column(
            modifier = Modifier
                .fillMaxWidth(0.86f)
                .clip(RoundedCornerShape(18.dp))
                .background(SignalBlue)
                .padding(horizontal = 16.dp, vertical = 12.dp),
        ) {
            entry.text.takeIf(String::isNotBlank)?.let {
                Text(it, color = Paper, style = MaterialTheme.typography.bodyLarge)
            }
            val imageCount = if (entry.pendingImageCount > 0) {
                entry.pendingImageCount
            } else {
                entry.imageAssetRefs.size
            }
            if (imageCount > 0) {
                if (entry.text.isNotBlank()) Spacer(Modifier.height(6.dp))
                Text("[图片 × $imageCount]", color = Paper, style = MaterialTheme.typography.bodyMedium)
            }
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    if (local && !sending && !preparing && deliveryNotice != null) deliveryNotice
                    else if (local && !sending && !preparing) "历史发送记录·不会自动重发"
                    else outboxStatusText(entry, preparing, now, sending),
                    color = Paper,
                    style = MaterialTheme.typography.labelSmall,
                )
                if (entry.state == OutboxState.RETRYABLE && !sending &&
                    (if (local) deliveryNotice != null else !waiting)) {
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "重试",
                        color = Paper,
                        style = MaterialTheme.typography.labelMedium,
                        modifier = Modifier.clickable { onRetry(entry.key) },
                    )
                }
                if (
                    !local && (entry.state == OutboxState.NEEDS_ATTACHMENTS && !preparing ||
                    entry.state == OutboxState.REJECTED && entry.imageAssetRefs.isNotEmpty())
                ) {
                    TextButton(
                        onClick = { onDiscard(entry.key) },
                        modifier = Modifier.height(48.dp),
                    ) {
                        Text("移除", color = Paper)
                    }
                }
            }
        }
    }
}

internal data class ThreadScrollPosition(val index: Int = 0, val offset: Int = 0)

/**
 * Keeps a conversation pinned when its prior last message is still visible.
 *
 * A media card can be taller than the viewport. When the next assistant message is appended,
 * checking proximity against the new item count makes that prior last card look one item too far
 * from the bottom, so the final answer remains just off-screen. The previous timeline size maps to
 * the prior last message's LazyColumn index because index zero is the date header.
 */
internal fun shouldFollowTimelineChange(
    previousTimelineSize: Int,
    currentTimelineSize: Int,
    lastVisibleItemIndex: Int,
    wasFollowingLatest: Boolean = false,
): Boolean = currentTimelineSize > 0 && (
    wasFollowingLatest || previousTimelineSize <= 0 || lastVisibleItemIndex >= previousTimelineSize
)

internal data class ThreadUiKey(
    val sessionScope: String,
    val sessionGeneration: Long,
    val threadKey: ThreadKey,
)

internal class ThreadUiStateRegistry {
    private val drafts = mutableMapOf<ThreadUiKey, String>()
    private val scrollPositions = mutableMapOf<ThreadUiKey, ThreadScrollPosition>()

    fun draft(key: ThreadUiKey): String = drafts[key].orEmpty()

    fun updateDraft(key: ThreadUiKey, value: String) {
        drafts[key] = value
    }

    fun scroll(key: ThreadUiKey): ThreadScrollPosition =
        scrollPositions[key] ?: ThreadScrollPosition()

    fun saveScroll(key: ThreadUiKey, index: Int, offset: Int) {
        scrollPositions[key] = ThreadScrollPosition(index, offset)
    }
}

internal fun visibleConversationMessages(
    messages: List<RichMessage>,
    outbox: List<OutboxEntry>,
): List<RichMessage> {
    val pendingClientIds = outbox.mapTo(mutableSetOf(), OutboxEntry::clientMessageId)
    val hiddenFallbackIndexes = mutableSetOf<Int>()
    outbox.forEach { pending ->
        val fallbackText = listOfNotNull(
            pending.text.takeIf(String::isNotBlank),
            (pending.pendingImageCount.takeIf { it > 0 } ?: pending.imageAssetRefs.size)
                .takeIf { it > 0 }
                ?.let { "[图片 × $it]" },
        ).joinToString("\n")
        messages.indices
            .filter { index ->
                index !in hiddenFallbackIndexes &&
                    messages[index].role == MessageRole.USER &&
                    messages[index].clientMessageId == null &&
                    messages[index].orderedBlocks()
                        .joinToString("\n") { it.readableText() }
                        .trim() == fallbackText
            }
            .minByOrNull { index ->
                kotlin.math.abs(messages[index].createdAtMillis - pending.createdAtMillis)
            }
            ?.let(hiddenFallbackIndexes::add)
    }
    return messages.filterIndexed { index, message ->
        !(
            message.role == MessageRole.USER &&
                (
                    message.clientMessageId in pendingClientIds ||
                        index in hiddenFallbackIndexes
                    )
            )
    }
}

internal sealed interface ThreadTimelineEntry {
    val key: String
    val createdAtMillis: Long
    val userFirstRank: Int

    data class Canonical(val message: RichMessage) : ThreadTimelineEntry {
        override val key = "message-${message.id}"
        override val createdAtMillis = message.createdAtMillis
        override val userFirstRank = if (message.role == MessageRole.USER) 0 else 1
    }

    data class Process(
        val turnId: String,
        val messages: List<RichMessage>,
    ) : ThreadTimelineEntry {
        override val key = "process-$turnId"
        override val createdAtMillis = messages.minOf(RichMessage::createdAtMillis)
        override val userFirstRank = 1
    }

    data class Accepted(val message: AcceptedUserMessage) : ThreadTimelineEntry {
        override val key = "accepted-${message.clientMessageId}"
        override val createdAtMillis = message.createdAtMillis
        override val userFirstRank = 0
    }

    data class Pending(val message: OutboxEntry) : ThreadTimelineEntry {
        override val key = "outbox-${message.clientMessageId}"
        override val createdAtMillis = message.createdAtMillis
        override val userFirstRank = 0
    }
}

internal fun conversationTimeline(
    messages: List<RichMessage>,
    accepted: List<AcceptedUserMessage>,
    pending: List<OutboxEntry>,
): List<ThreadTimelineEntry> = buildList {
    val processByTurn = messages
        .filter { it.presentation == MessagePresentation.PROCESS }
        .groupBy { it.turnId ?: "message-${it.id}" }
    val emittedProcessTurns = mutableSetOf<String>()
    messages.forEach { message ->
        if (message.presentation == MessagePresentation.PROCESS) {
            val turnId = message.turnId ?: "message-${message.id}"
            if (emittedProcessTurns.add(turnId)) {
                add(ThreadTimelineEntry.Process(turnId, processByTurn.getValue(turnId)))
            }
        } else {
            add(ThreadTimelineEntry.Canonical(message))
        }
    }
    accepted.forEach { add(ThreadTimelineEntry.Accepted(it)) }
    pending.forEach { add(ThreadTimelineEntry.Pending(it)) }
}.withIndex().sortedWith(
    compareBy<IndexedValue<ThreadTimelineEntry>>(
        { it.value.createdAtMillis },
        { it.value.userFirstRank },
        IndexedValue<ThreadTimelineEntry>::index,
    ),
).map(IndexedValue<ThreadTimelineEntry>::value)

@Composable
internal fun PairingScreen(
    isPairing: Boolean,
    error: String?,
    canCancel: Boolean,
    addressOnly: Boolean,
    onCancel: () -> Unit,
    onPair: (String, String, String) -> Unit,
    onChangeServer: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var relayOrigin by rememberSaveable { mutableStateOf("") }
    // Enrollment credentials must not enter the Activity saved-state bundle.
    var accessKey by remember { mutableStateOf("") }
    var secretKey by remember { mutableStateOf("") }
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 24.dp, vertical = 28.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text(
            if (addressOnly) "更换服务器地址" else "绑定你的 Codex 助手",
            style = MaterialTheme.typography.headlineSmall,
        )
        Text(
            if (addressOnly) {
                "输入新的 Relay 服务器地址。验证为同一个 Codex 后才会切换。"
            } else {
                "输入服务器地址、AK 和 SK。凭据由 Android 系统安全存储保护。"
            },
            color = Muted,
            style = MaterialTheme.typography.bodyMedium,
        )
        OutlinedTextField(
            value = relayOrigin,
            onValueChange = { relayOrigin = it },
            enabled = !isPairing,
            modifier = Modifier.fillMaxWidth(),
            label = { Text("Relay 服务器地址") },
            placeholder = {
                Text("https://你的服务器IP:${BuildConfig.RELAY_SERVICE_PORT}")
            },
            singleLine = true,
        )
        if (!addressOnly) {
            OutlinedTextField(
                value = accessKey,
                onValueChange = { accessKey = it },
                enabled = !isPairing,
                modifier = Modifier.fillMaxWidth(),
                label = { Text("AK") },
                singleLine = true,
            )
            OutlinedTextField(
                value = secretKey,
                onValueChange = { secretKey = it },
                enabled = !isPairing,
                modifier = Modifier.fillMaxWidth().semantics { password() },
                label = { Text("SK") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(
                    keyboardType = KeyboardType.Password,
                    autoCorrectEnabled = false,
                ),
                visualTransformation = PasswordVisualTransformation(),
            )
        }
        error?.let {
            Text(it, color = RiskRed, style = MaterialTheme.typography.bodyMedium)
        }
        Button(
            onClick = {
                if (addressOnly) onChangeServer(relayOrigin)
                else onPair(relayOrigin, accessKey, secretKey)
            },
            enabled = !isPairing && relayOrigin.trim().startsWith("https://") &&
                (addressOnly || (accessKey.length >= 8 && secretKey.length >= 32)),
            modifier = Modifier.fillMaxWidth().height(52.dp),
        ) {
            if (isPairing) {
                CircularProgressIndicator(modifier = Modifier.size(22.dp), strokeWidth = 2.dp, color = Paper)
            } else {
                Text(if (addressOnly) "验证并切换" else "安全绑定")
            }
        }
        if (canCancel) {
            TextButton(onClick = onCancel, modifier = Modifier.fillMaxWidth()) { Text("返回会话") }
        }
    }
}

@Composable
private fun Composer(
    draft: String,
    enabled: Boolean,
    recovering: Boolean,
    selectedImages: List<PendingImageAttachment>,
    localImageEnabled: Boolean,
    maxImages: Int,
    preparing: Boolean,
    attachmentError: String?,
    canStop: Boolean,
    stopping: Boolean,
    stopError: String?,
    onStop: () -> Unit,
    onPickImages: () -> Unit,
    onRemoveImage: (String) -> Unit,
    onDraftChange: (String) -> Unit,
    onSend: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(Paper)
            .padding(horizontal = 12.dp, vertical = 10.dp),
    ) {
        if (selectedImages.isNotEmpty()) {
            EvidenceFilmstrip(
                images = selectedImages,
                maxImages = maxImages,
                canRemove = !preparing,
                onRemoveImage = onRemoveImage,
            )
            Spacer(Modifier.height(8.dp))
        }
        attachmentError?.let { message ->
            Text(message, color = RiskRed, style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.height(8.dp))
        }
        stopError?.let { message ->
            Text(message, color = RiskRed, style = MaterialTheme.typography.bodySmall)
        }
        if (canStop) {
            TextButton(
                onClick = onStop,
                enabled = !stopping,
                modifier = Modifier.align(Alignment.End).testTag("stop_turn"),
            ) {
                Text(if (stopping) "正在请求停止…" else "停止本轮")
            }
        }
        Row(
            verticalAlignment = Alignment.Bottom,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            if (localImageEnabled && selectedImages.size < maxImages) {
                IconButton(
                    onClick = onPickImages,
                    enabled = enabled && !preparing,
                    modifier = Modifier
                        .size(52.dp)
                        .border(1.dp, SignalBlue.copy(alpha = 0.45f), RoundedCornerShape(16.dp)),
                ) {
                    Icon(Icons.Default.Add, contentDescription = "添加图片证据", tint = SignalBlue)
                }
            }
            OutlinedTextField(
                value = draft,
                onValueChange = onDraftChange,
                enabled = enabled && !preparing,
                modifier = Modifier
                    .weight(1f)
                    .testTag("message_composer")
                    .semantics { contentDescription = "消息输入框" },
                placeholder = {
                    Text(
                        when {
                            preparing -> "正在上传视觉证据…"
                            enabled -> "发送消息"
                            recovering -> "正在安全恢复会话…"
                            else -> "先新建一个会话"
                        },
                    )
                },
                shape = RoundedCornerShape(16.dp),
                minLines = 1,
                maxLines = 4,
            )
            FilledIconButton(
                onClick = onSend,
                enabled = enabled && !preparing && (draft.isNotBlank() || selectedImages.isNotEmpty()),
                modifier = Modifier.size(52.dp).testTag("send_message"),
            ) {
                Icon(Icons.AutoMirrored.Filled.Send, contentDescription = "发送")
            }
        }
    }
}

@Composable
internal fun EvidenceFilmstrip(
    images: List<PendingImageAttachment>,
    maxImages: Int,
    canRemove: Boolean,
    onRemoveImage: (String) -> Unit,
) {
    Column {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "视觉证据",
                color = Ink,
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.weight(1f))
            Text(
                evidenceCounterText(images.size, maxImages),
                color = SignalBlue,
                style = MaterialTheme.typography.labelMedium,
            )
        }
        Spacer(Modifier.height(6.dp))
        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            items(images, key = { it.clientAssetId }) { image ->
                val index = images.indexOfFirst { it.clientAssetId == image.clientAssetId }
                EvidenceFrame(
                    image = image,
                    index = index.coerceAtLeast(0),
                    canRemove = canRemove,
                    onRemove = { onRemoveImage(image.clientAssetId) },
                )
            }
        }
    }
}

internal fun evidenceCounterText(count: Int, maxImages: Int): String {
    val safeMaximum = maxImages.coerceAtLeast(0)
    return "${count.coerceIn(0, safeMaximum)} / $safeMaximum"
}

internal fun outboxStatusText(
    entry: OutboxEntry,
    preparing: Boolean,
    nowMillis: Long,
    sending: Boolean = false,
): String = when (entry.state) {
    OutboxState.QUEUED -> if (sending) "发送中" else "等待发送"
    OutboxState.ACCEPTED -> "处理中"
    OutboxState.WAITING_TURN -> "等待上一条完成"
    OutboxState.RETRYABLE -> if (entry.retryNotBeforeMillis > nowMillis) {
        val seconds = ((entry.retryNotBeforeMillis - nowMillis + 999) / 1_000).coerceAtLeast(1)
        "请等待 ${seconds} 秒后重试"
    } else {
        "发送失败·可重试"
    }
    OutboxState.NEEDS_SYNC -> "状态待同步·暂停重试"
    OutboxState.REJECTED -> if (entry.imageAssetRefs.isNotEmpty()) {
        "图片已不可用，请移除此消息后重新选择"
    } else {
        "未发送·请检查内容"
    }
    OutboxState.NEEDS_ATTACHMENTS -> if (preparing) {
        "正在安全上传图片…"
    } else {
        "图片需重新选择，文字未单独发送"
    }
}

@Composable
private fun EvidenceFrame(
    image: PendingImageAttachment,
    index: Int,
    canRemove: Boolean,
    onRemove: () -> Unit,
) {
    val statusText = attachmentStatusText(image.uploadState)
    val bitmap = remember(image.clientAssetId) {
        BitmapFactory.decodeByteArray(image.thumbnailBytes, 0, image.thumbnailBytes.size)
    }
    DisposableEffect(bitmap) {
        onDispose { bitmap?.takeUnless { it.isRecycled }?.recycle() }
    }
    Box(
        modifier = Modifier
            .size(width = 92.dp, height = 76.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(Canvas)
            .border(1.dp, SignalBlue.copy(alpha = 0.65f), RoundedCornerShape(12.dp))
            .semantics {
                contentDescription = "第 ${index + 1} 张视觉证据，$statusText"
            },
    ) {
        bitmap?.let {
            Image(
                bitmap = it.asImageBitmap(),
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        }
        Text(
            text = (index + 1).toString().padStart(2, '0'),
            color = Paper,
            style = MaterialTheme.typography.labelSmall,
            modifier = Modifier
                .align(Alignment.TopStart)
                .background(Ink.copy(alpha = 0.78f), RoundedCornerShape(bottomEnd = 8.dp))
                .padding(horizontal = 7.dp, vertical = 4.dp),
        )
        if (canRemove) {
            IconButton(
                onClick = onRemove,
                modifier = Modifier.align(Alignment.TopEnd).size(48.dp),
            ) {
                Icon(
                    Icons.Default.Close,
                    contentDescription = "移除第 ${index + 1} 张图片",
                    tint = Paper,
                    modifier = Modifier
                        .size(22.dp)
                        .background(Ink.copy(alpha = 0.72f), CircleShape)
                        .padding(3.dp),
                )
            }
        }
        Row(
            modifier = Modifier
                .align(Alignment.BottomStart)
                .background(Ink.copy(alpha = 0.82f), RoundedCornerShape(topEnd = 8.dp))
                .padding(end = 7.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                Modifier
                    .width(4.dp)
                    .height(20.dp)
                    .background(attachmentStatusColor(image.uploadState)),
            )
            Spacer(Modifier.width(5.dp))
            Text(
                text = statusText,
                color = Paper,
                style = MaterialTheme.typography.labelSmall,
                maxLines = 1,
            )
        }
    }
}

internal fun attachmentStatusText(state: AttachmentUploadState): String = when (state) {
    AttachmentUploadState.SELECTED -> "已选择"
    AttachmentUploadState.UPLOADING -> "上传中"
    AttachmentUploadState.UPLOADED -> "已上传"
    AttachmentUploadState.FAILED -> "上传失败"
}

private fun attachmentStatusColor(state: AttachmentUploadState): Color = when (state) {
    AttachmentUploadState.SELECTED -> SignalBlue
    AttachmentUploadState.UPLOADING -> WarningAmber
    AttachmentUploadState.UPLOADED -> CompleteGreen
    AttachmentUploadState.FAILED -> RiskRed
}

@Composable
private fun ConnectionBadge(
    state: GatewayConnectionState,
    kind: GatewayKind,
    embeddedRuntimeStatus: EmbeddedRuntimeStatus,
) {
    val localProblem = kind == GatewayKind.LOCAL && embeddedRuntimeStatus in setOf(
        EmbeddedRuntimeStatus.READY_NEEDS_AUTH,
        EmbeddedRuntimeStatus.FAILED,
        EmbeddedRuntimeStatus.UNAVAILABLE,
    )
    val color = if (kind == GatewayKind.UNCONFIGURED) Muted else if (localProblem) {
        WarningAmber
    } else when (state) {
        GatewayConnectionState.ONLINE -> CompleteGreen
        GatewayConnectionState.CONNECTING -> SignalBlue
        GatewayConnectionState.OFFLINE -> WarningAmber
    }
    val text = when (kind) {
        GatewayKind.LOCAL -> when (embeddedRuntimeStatus) {
            EmbeddedRuntimeStatus.PREPARING -> "正在展开内置运行环境"
            EmbeddedRuntimeStatus.INSTALLING -> "正在离线安装内置 Codex"
            EmbeddedRuntimeStatus.READY_NEEDS_AUTH -> "请在更多菜单配置模型凭证"
            EmbeddedRuntimeStatus.FAILED -> "本机运行环境安装失败"
            EmbeddedRuntimeStatus.UNAVAILABLE -> "本机运行环境不可用"
            EmbeddedRuntimeStatus.READY -> when (state) {
                GatewayConnectionState.ONLINE -> "本机 Codex 已连接"
                GatewayConnectionState.CONNECTING -> "正在启动本机 Codex"
                GatewayConnectionState.OFFLINE -> "本机 Codex 暂不可用"
            }
        }
        GatewayKind.DEMO -> when (state) {
            GatewayConnectionState.ONLINE -> "Demo · 本地"
            GatewayConnectionState.CONNECTING -> "Demo 启动中"
            GatewayConnectionState.OFFLINE -> "Demo · 离线"
        }
        GatewayKind.RELAY, GatewayKind.REMOTE -> when (state) {
            GatewayConnectionState.ONLINE -> "服务已连接"
            GatewayConnectionState.CONNECTING -> "正在连接"
            GatewayConnectionState.OFFLINE -> "暂时离线"
        }
        GatewayKind.UNCONFIGURED -> "未配对"
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(7.dp).background(color, CircleShape))
        Spacer(Modifier.width(4.dp))
        Text(text, color = color, style = MaterialTheme.typography.labelMedium)
    }
}

@Composable
private fun RecoveringConversation(modifier: Modifier = Modifier) {
    Column(
        modifier = modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        CircularProgressIndicator(modifier = Modifier.size(30.dp), strokeWidth = 3.dp)
        Spacer(Modifier.height(14.dp))
        Text("正在安全恢复会话", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.height(6.dp))
        Text("同步完成前暂停发送，避免消息进入错误的会话。", color = Muted)
    }
}

@Composable
private fun EmptyConversation(
    onCreate: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(
            modifier = Modifier.size(64.dp).clip(RoundedCornerShape(18.dp)).background(Ink),
            contentAlignment = Alignment.Center,
        ) {
            Text("C", color = Paper, style = MaterialTheme.typography.headlineSmall)
        }
        Spacer(Modifier.height(18.dp))
        Text("开始与专属 Codex 对话", style = MaterialTheme.typography.titleLarge)
        Spacer(Modifier.height(8.dp))
        Text("每个会话都会对齐一个独立的 Codex Thread。", color = Muted)
        Spacer(Modifier.height(20.dp))
        Button(onClick = onCreate) { Text("新建会话") }
    }
}

@Composable
private fun ThreadDrawer(
    threads: List<CodexThread>,
    currentThreadKey: ThreadKey?,
    onSelect: (ThreadKey) -> Unit,
    onCreate: () -> Unit,
    onRename: (ThreadKey, String) -> Unit,
    onArchive: (ThreadKey) -> Unit,
    onDelete: (ThreadKey) -> Unit,
) {
    var renameTarget by remember { mutableStateOf<CodexThread?>(null) }
    var deleteTarget by remember { mutableStateOf<CodexThread?>(null) }

    ModalDrawerSheet(
        modifier = Modifier.fillMaxHeight().width(340.dp),
        drawerContainerColor = Paper,
    ) {
        Column(Modifier.padding(horizontal = 18.dp, vertical = 16.dp)) {
            Text("会话", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.height(14.dp))
            Button(onClick = onCreate, modifier = Modifier.fillMaxWidth()) {
                Icon(Icons.Default.Add, contentDescription = null)
                Spacer(Modifier.width(8.dp))
                Text("新建会话")
            }
        }
        HorizontalDivider(color = ChebyDivider)
        LazyColumn(
            modifier = Modifier.fillMaxSize(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 8.dp),
        ) {
            items(threads, key = { it.key.value }) { thread ->
                ThreadRow(
                    thread = thread,
                    selected = thread.key == currentThreadKey,
                    onSelect = { onSelect(thread.key) },
                    onRename = { renameTarget = thread },
                    onArchive = { onArchive(thread.key) },
                    onDelete = { deleteTarget = thread },
                )
            }
        }
    }

    renameTarget?.let { thread ->
        RenameDialog(
            initialTitle = thread.title,
            onDismiss = { renameTarget = null },
            onConfirm = { title ->
                onRename(thread.key, title)
                renameTarget = null
            },
        )
    }

    deleteTarget?.let { thread ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            title = { Text("永久删除会话？") },
            text = { Text("「${thread.title}」将无法恢复。若只想暂时隐藏，请使用归档。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        onDelete(thread.key)
                        deleteTarget = null
                    },
                ) { Text("永久删除", color = RiskRed) }
            },
            dismissButton = { TextButton(onClick = { deleteTarget = null }) { Text("取消") } },
        )
    }
}

@Composable
private fun ThreadRow(
    thread: CodexThread,
    selected: Boolean,
    onSelect: () -> Unit,
    onRename: () -> Unit,
    onArchive: () -> Unit,
    onDelete: () -> Unit,
) {
    var menuExpanded by remember { mutableStateOf(false) }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(if (selected) SignalBlue.copy(alpha = 0.08f) else Color.Transparent)
            .clickable(onClick = onSelect)
            .padding(start = 18.dp, top = 12.dp, bottom = 12.dp, end = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier.size(10.dp).background(threadStateColor(thread.state), CircleShape),
        )
        Spacer(Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    thread.title,
                    modifier = Modifier.weight(1f),
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                if (selected) Icon(Icons.Default.Check, null, tint = SignalBlue, modifier = Modifier.size(18.dp))
            }
            Text(
                thread.preview,
                color = Muted,
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (thread.state != ThreadRunState.IDLE) {
                Text(threadStateLabel(thread.state), color = threadStateColor(thread.state), style = MaterialTheme.typography.labelMedium)
            }
        }
        Box {
            IconButton(onClick = { menuExpanded = true }) {
                Icon(Icons.Default.MoreVert, contentDescription = "会话操作")
            }
            DropdownMenu(expanded = menuExpanded, onDismissRequest = { menuExpanded = false }) {
                DropdownMenuItem(
                    text = { Text("重命名") },
                    leadingIcon = { Icon(Icons.Default.Edit, null) },
                    onClick = { menuExpanded = false; onRename() },
                )
                DropdownMenuItem(
                    text = { Text("归档") },
                    onClick = { menuExpanded = false; onArchive() },
                )
                DropdownMenuItem(
                    text = { Text("永久删除", color = RiskRed) },
                    onClick = { menuExpanded = false; onDelete() },
                )
            }
        }
    }
}

@Composable
private fun RenameDialog(
    initialTitle: String,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit,
) {
    var title by rememberSaveable(initialTitle) { mutableStateOf(initialTitle) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("重命名会话") },
        text = {
            OutlinedTextField(
                value = title,
                onValueChange = { title = it },
                singleLine = true,
                label = { Text("会话名称") },
            )
        },
        confirmButton = {
            TextButton(onClick = { if (title.isNotBlank()) onConfirm(title) }) { Text("保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

private fun threadStateColor(state: ThreadRunState): Color = when (state) {
    ThreadRunState.IDLE -> CompleteGreen
    ThreadRunState.RUNNING -> SignalBlue
    ThreadRunState.WAITING_USER -> WarningAmber
    ThreadRunState.WAITING_APPROVAL -> WarningAmber
    ThreadRunState.FAILED -> RiskRed
}

private fun threadStateLabel(state: ThreadRunState): String = when (state) {
    ThreadRunState.IDLE -> "空闲"
    ThreadRunState.RUNNING -> "执行中"
    ThreadRunState.WAITING_USER -> "等待输入"
    ThreadRunState.WAITING_APPROVAL -> "等待批准"
    ThreadRunState.FAILED -> "需要重试"
}
