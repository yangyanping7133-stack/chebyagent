package com.cheby.codex.mobile.ui.rich

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.cheby.codex.mobile.model.ActionItem
import com.cheby.codex.mobile.model.ActionKind
import com.cheby.codex.mobile.model.ActionsBlock
import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.FactsBlock
import com.cheby.codex.mobile.model.DiffBlock
import com.cheby.codex.mobile.model.EphemeralMediaStore
import com.cheby.codex.mobile.model.FileBlock
import com.cheby.codex.mobile.model.FileChangeKind
import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import com.cheby.codex.mobile.model.MessageRole
import com.cheby.codex.mobile.model.MediaBlock
import com.cheby.codex.mobile.model.MetricsBlock
import com.cheby.codex.mobile.model.NoticeBlock
import com.cheby.codex.mobile.model.RelayBlock
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import com.cheby.codex.mobile.model.RichBlock
import com.cheby.codex.mobile.model.RichMessage
import com.cheby.codex.mobile.model.RichMessageState
import com.cheby.codex.mobile.model.SourceBlock
import com.cheby.codex.mobile.model.StatusBlock
import com.cheby.codex.mobile.model.StepState
import com.cheby.codex.mobile.model.StepsBlock
import com.cheby.codex.mobile.model.TerminalBlock
import com.cheby.codex.mobile.model.TestBlock
import com.cheby.codex.mobile.model.TextBlock
import com.cheby.codex.mobile.model.Tone
import com.cheby.codex.mobile.model.ToolBlock
import com.cheby.codex.mobile.model.UnknownBlock
import com.cheby.codex.mobile.model.orderedBlocks
import com.cheby.codex.mobile.model.readableText
import com.cheby.codex.mobile.ui.theme.AmberSoft
import com.cheby.codex.mobile.ui.theme.BlueSoft
import com.cheby.codex.mobile.ui.theme.CompleteGreen
import com.cheby.codex.mobile.ui.theme.Divider
import com.cheby.codex.mobile.ui.theme.GreenSoft
import com.cheby.codex.mobile.ui.theme.Ink
import com.cheby.codex.mobile.ui.theme.Muted
import com.cheby.codex.mobile.ui.theme.Paper
import com.cheby.codex.mobile.ui.theme.RedSoft
import com.cheby.codex.mobile.ui.theme.RiskRed
import com.cheby.codex.mobile.ui.theme.SignalBlue
import com.cheby.codex.mobile.ui.theme.WarningAmber
import kotlin.math.max
import kotlinx.coroutines.delay

@Composable
fun MessageRow(
    message: RichMessage,
    onAction: (String) -> Unit,
    modifier: Modifier = Modifier,
    actionRetryNotBeforeMillis: Map<String, Long> = emptyMap(),
) {
    if (message.role == MessageRole.USER) {
        Row(
            modifier = modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.End,
        ) {
            Column(
                modifier = Modifier
                    .widthIn(max = 330.dp)
                    .clip(RoundedCornerShape(16.dp, 16.dp, 4.dp, 16.dp))
                    .background(SignalBlue)
                    .padding(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                val ordered = message.orderedBlocks()
                ordered.filterNot { it is MediaBlock }.forEach { block ->
                    Text(
                        text = block.readableText(),
                        color = Paper,
                        style = MaterialTheme.typography.bodyLarge,
                    )
                }
                ordered.filterIsInstance<MediaBlock>()
                    .takeIf(List<MediaBlock>::isNotEmpty)
                    ?.let { UserMediaStrip(it) }
            }
        }
        return
    }

    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.Top,
    ) {
        Box(
            modifier = Modifier
                .size(36.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(Ink),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                if (message.role == MessageRole.SYSTEM) "!" else "C",
                color = Paper,
                fontWeight = FontWeight.Bold,
            )
        }
        Spacer(Modifier.width(10.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = if (message.role == MessageRole.SYSTEM) "系统提示" else "ChebyAgent",
                color = Muted,
                style = MaterialTheme.typography.labelMedium,
                modifier = Modifier.padding(start = 2.dp, bottom = 6.dp),
            )
            RichMessageCard(
                message = message,
                onAction = onAction,
                actionRetryNotBeforeMillis = actionRetryNotBeforeMillis,
            )
        }
    }
}

@Composable
private fun UserMediaStrip(blocks: List<MediaBlock>) {
    var fullScreenBlockId by remember(blocks.map(MediaBlock::id)) { mutableStateOf<String?>(null) }
    LazyRow(
        modifier = Modifier.testTag("user_source_pages"),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        itemsIndexed(blocks, key = { _, block -> block.id }) { index, block ->
            UserMediaThumbnail(block, index) { fullScreenBlockId = block.id }
        }
    }
    val fullScreenBlock = blocks.firstOrNull { it.id == fullScreenBlockId }
    if (fullScreenBlock != null) {
        val title = fullScreenBlock.caption?.takeIf(String::isNotBlank) ?: "原始图片"
        val fallback = fullScreenBlock.fallbackText.takeIf(String::isNotBlank) ?: "原始图片暂不可用"
        val bitmap = remember(fullScreenBlock.assetId) { decodeEphemeralMedia(fullScreenBlock.assetId) }
        DisposableEffect(bitmap) {
            onDispose { bitmap?.takeUnless(Bitmap::isRecycled)?.recycle() }
        }
        Dialog(
            onDismissRequest = { fullScreenBlockId = null },
            properties = DialogProperties(usePlatformDefaultWidth = false),
        ) {
            Box(modifier = Modifier.fillMaxSize().background(Ink)) {
                if (bitmap != null) {
                    Image(
                        bitmap = bitmap.asImageBitmap(),
                        contentDescription = title,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier.fillMaxSize().padding(16.dp),
                    )
                } else {
                    Text(
                        fallback,
                        color = Paper,
                        style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.align(Alignment.Center).padding(24.dp),
                    )
                }
                IconButton(
                    onClick = { fullScreenBlockId = null },
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(16.dp)
                        .background(Paper.copy(alpha = 0.18f), CircleShape),
                ) {
                    Icon(Icons.Default.Close, contentDescription = "关闭原始图片预览", tint = Paper)
                }
            }
        }
    }
}

@Composable
private fun UserMediaThumbnail(block: MediaBlock, index: Int, onOpen: () -> Unit) {
    val bitmap = remember(block.assetId) {
        decodeEphemeralMedia(
            block.assetId,
            maxDecodedEdge = MAX_USER_MEDIA_PREVIEW_EDGE,
            maxDecodedPixels = MAX_USER_MEDIA_PREVIEW_PIXELS,
        )
    }
    DisposableEffect(bitmap) {
        onDispose { bitmap?.takeUnless(Bitmap::isRecycled)?.recycle() }
    }
    val title = block.caption?.takeIf(String::isNotBlank) ?: "原始图片 ${index + 1}"
    val fallback = block.fallbackText.takeIf(String::isNotBlank) ?: "原始图片暂不可用"
    Box(
        modifier = Modifier
            .size(width = 132.dp, height = 104.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(Ink.copy(alpha = 0.32f))
            .clickable(onClickLabel = "全屏查看原始图片") { onOpen() },
    ) {
        if (bitmap != null) {
            Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = title,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Text(
                fallback,
                color = Paper.copy(alpha = 0.82f),
                style = MaterialTheme.typography.labelSmall,
                modifier = Modifier.align(Alignment.Center).padding(8.dp),
            )
        }
        Text(
            (index + 1).toString().padStart(2, '0'),
            color = Paper,
            style = MaterialTheme.typography.labelSmall,
            modifier = Modifier
                .align(Alignment.TopStart)
                .background(Ink.copy(alpha = 0.78f), RoundedCornerShape(bottomEnd = 7.dp))
                .padding(horizontal = 7.dp, vertical = 4.dp),
        )
    }
}

@Composable
private fun RichMessageCard(
    message: RichMessage,
    onAction: (String) -> Unit,
    actionRetryNotBeforeMillis: Map<String, Long>,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = Paper),
        border = androidx.compose.foundation.BorderStroke(1.dp, Divider),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            message.orderedBlocks().forEach { block ->
                RichBlockView(block, onAction, actionRetryNotBeforeMillis)
            }
            if (message.state == RichMessageState.STREAMING && message.blocks["status"] == null) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
            }
        }
    }
}

@Composable
private fun RichBlockView(
    block: RichBlock,
    onAction: (String) -> Unit,
    actionRetryNotBeforeMillis: Map<String, Long>,
) {
    when (block) {
        is TextBlock -> {
            val modifier = if (block.id == "answer") Modifier.testTag("codex_answer") else Modifier
            if (block.markdown) {
                SafeMarkdownText(block.text, modifier)
            } else {
                Text(block.text, modifier = modifier, style = MaterialTheme.typography.bodyLarge)
            }
        }
        is StatusBlock -> StatusView(block)
        is StepsBlock -> StepsView(block)
        is MetricsBlock -> MetricsView(block)
        is FactsBlock -> FactsView(block)
        is MediaBlock -> MediaView(block)
        is SourceBlock -> SourceView(block)
        is RelayBlock -> RelayView(block)
        is NoticeBlock -> NoticeView(block)
        is ActionsBlock -> ActionsView(block, onAction, actionRetryNotBeforeMillis)
        is TerminalBlock -> TerminalView(block)
        is FileBlock -> FileView(block)
        is DiffBlock -> DiffView(block)
        is ToolBlock -> ToolView(block)
        is TestBlock -> TestView(block)
        is InteractiveCollectionBlock -> InteractiveCollectionView(block)
        is ReferenceGalleryBlock -> ReferenceGalleryView(block)
        is ComparisonBlock -> ComparisonView(block)
        is BilingualCardBlock -> BilingualCardView(block)
        is UnknownBlock -> UnknownView(block)
    }
}

@Composable
private fun MediaView(block: MediaBlock) {
    var fullScreen by remember(block.id) { mutableStateOf(false) }
    val bitmap = remember(block.assetId) { decodeEphemeralMedia(block.assetId) }
    DisposableEffect(bitmap) {
        onDispose { bitmap?.takeUnless(Bitmap::isRecycled)?.recycle() }
    }
    val title = block.caption?.takeIf(String::isNotBlank) ?: "图片"
    val fallback = block.fallbackText.takeIf(String::isNotBlank) ?: "媒体内容暂不可用"
    if (bitmap != null) {
        val aspectRatio = (bitmap.width.toFloat() / bitmap.height.coerceAtLeast(1).toFloat())
            .coerceIn(MIN_MEDIA_ASPECT_RATIO, MAX_MEDIA_ASPECT_RATIO)
        Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
            Image(
                bitmap = bitmap.asImageBitmap(),
                contentDescription = title,
                contentScale = ContentScale.Fit,
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(aspectRatio)
                    .heightIn(max = 520.dp)
                    .clip(RoundedCornerShape(12.dp))
                    .background(Ink)
                    .clickable(onClickLabel = "全屏查看图片") { fullScreen = true },
            )
            Text(title, color = Ink, style = MaterialTheme.typography.bodyMedium)
            Text("点按图片可全屏查看", color = Muted, style = MaterialTheme.typography.labelMedium)
        }
        if (fullScreen) {
            Dialog(
                onDismissRequest = { fullScreen = false },
                properties = DialogProperties(usePlatformDefaultWidth = false),
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(Ink),
                ) {
                    Image(
                        bitmap = bitmap.asImageBitmap(),
                        contentDescription = title,
                        contentScale = ContentScale.Fit,
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(16.dp),
                    )
                    IconButton(
                        onClick = { fullScreen = false },
                        modifier = Modifier
                            .align(Alignment.TopEnd)
                            .padding(16.dp)
                            .background(Paper.copy(alpha = 0.18f), CircleShape),
                    ) {
                        Icon(Icons.Default.Close, contentDescription = "关闭全屏预览", tint = Paper)
                    }
                }
            }
        }
        return
    }

    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .aspectRatio(16f / 9f)
                .clip(RoundedCornerShape(12.dp))
                .background(Ink)
                .semantics(mergeDescendants = true) {
                    contentDescription = "媒体占位：$title。$fallback"
                    role = Role.Button
                }
                .clickable(onClickLabel = "全屏查看媒体占位") { fullScreen = true },
        ) {
            MediaArtwork(Modifier.fillMaxSize())
            Column(
                modifier = Modifier
                    .align(Alignment.BottomStart)
                    .padding(14.dp),
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                Text(
                    text = "UNAVAILABLE",
                    color = SignalBlue,
                    style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    text = title,
                    color = Paper,
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        Text(fallback, color = Ink, style = MaterialTheme.typography.bodyMedium)
        Text("安全媒体解析尚未接入 · 点按查看占位", color = Muted, style = MaterialTheme.typography.labelMedium)
    }

    if (fullScreen) {
        Dialog(
            onDismissRequest = { fullScreen = false },
            properties = DialogProperties(usePlatformDefaultWidth = false),
        ) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Ink)
                    .semantics(mergeDescendants = false) {
                        contentDescription = "全屏媒体占位：$title。$fallback"
                    },
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .aspectRatio(16f / 9f)
                        .align(Alignment.Center),
                ) {
                    MediaArtwork(Modifier.fillMaxSize())
                }
                IconButton(
                    onClick = { fullScreen = false },
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(16.dp)
                        .background(Paper.copy(alpha = 0.12f), CircleShape),
                ) {
                    Icon(Icons.Default.Close, contentDescription = "关闭全屏预览", tint = Paper)
                }
                Column(
                    modifier = Modifier
                        .align(Alignment.BottomStart)
                        .padding(24.dp),
                    verticalArrangement = Arrangement.spacedBy(5.dp),
                ) {
                    Text("媒体内容暂不可用", color = SignalBlue, style = MaterialTheme.typography.labelLarge)
                    Text(title, color = Paper, style = MaterialTheme.typography.titleLarge)
                    Text(fallback, color = Paper.copy(alpha = 0.78f), style = MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }
}

private fun decodeEphemeralMedia(
    assetId: String,
    maxDecodedEdge: Int = MAX_DECODED_MEDIA_EDGE,
    maxDecodedPixels: Long = MAX_DECODED_MEDIA_PIXELS,
): Bitmap? {
    val asset = EphemeralMediaStore.get(assetId) ?: return null
    val bytes = asset.bytes
    return try {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        val width = bounds.outWidth
        val height = bounds.outHeight
        if (
            width <= 0 || height <= 0 ||
            width > MAX_MEDIA_EDGE || height > MAX_MEDIA_EDGE ||
            width.toLong() * height.toLong() > MAX_MEDIA_SOURCE_PIXELS
        ) {
            null
        } else {
            var sampleSize = 1
            while (
                maxOf(width, height) / sampleSize > maxDecodedEdge ||
                width.toLong() * height.toLong() / sampleSize / sampleSize > maxDecodedPixels
            ) {
                sampleSize *= 2
            }
            BitmapFactory.decodeByteArray(
                bytes,
                0,
                bytes.size,
                BitmapFactory.Options().apply {
                    inSampleSize = sampleSize
                    inPreferredConfig = Bitmap.Config.ARGB_8888
                },
            )
        }
    } catch (_: OutOfMemoryError) {
        null
    } catch (_: RuntimeException) {
        null
    } finally {
        bytes.fill(0)
    }
}

@Composable
private fun MediaArtwork(modifier: Modifier = Modifier) {
    Canvas(modifier = modifier) {
        drawRect(Ink)
        drawRoundRect(
            color = SignalBlue.copy(alpha = 0.28f),
            topLeft = Offset(size.width * 0.09f, size.height * 0.11f),
            size = Size(size.width * 0.72f, size.height * 0.62f),
            cornerRadius = CornerRadius(size.minDimension * 0.035f),
        )
        drawRoundRect(
            color = Paper.copy(alpha = 0.10f),
            topLeft = Offset(size.width * 0.31f, size.height * 0.24f),
            size = Size(size.width * 0.61f, size.height * 0.57f),
            cornerRadius = CornerRadius(size.minDimension * 0.025f),
        )
        val edge = size.minDimension * 0.055f
        val left = size.width * 0.08f
        val top = size.height * 0.10f
        val right = size.width * 0.92f
        val bottom = size.height * 0.90f
        listOf(
            Offset(left + edge, top) to Offset(left, top),
            Offset(left, top) to Offset(left, top + edge),
            Offset(right - edge, top) to Offset(right, top),
            Offset(right, top) to Offset(right, top + edge),
            Offset(left, bottom - edge) to Offset(left, bottom),
            Offset(left, bottom) to Offset(left + edge, bottom),
            Offset(right - edge, bottom) to Offset(right, bottom),
            Offset(right, bottom - edge) to Offset(right, bottom),
        ).forEach { (start, end) ->
            drawLine(Paper.copy(alpha = 0.72f), start, end, strokeWidth = 3.dp.toPx(), cap = StrokeCap.Round)
        }
    }
}

@Composable
private fun TerminalView(block: TerminalBlock) {
    var expanded by remember(block.id) { mutableStateOf(false) }
    val canExpand = evidenceNeedsExpansion(block.output, COLLAPSED_TERMINAL_LINES)
    val visibleOutput = evidencePreviewText(
        value = block.output,
        maxLines = if (expanded) EXPANDED_EVIDENCE_LINES else COLLAPSED_TERMINAL_LINES,
        maxCodePoints = if (expanded) EXPANDED_EVIDENCE_CHARACTERS else COLLAPSED_EVIDENCE_CHARACTERS,
    )
    EvidenceFrame(
        tag = "TERM",
        title = block.title,
        state = block.state,
        detail = block.commandLabel,
    ) {
        if (block.output.isNotBlank()) {
            Text(
                text = visibleOutput,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(12.dp),
                color = Ink,
                style = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace),
                maxLines = if (expanded) EXPANDED_EVIDENCE_LINES else COLLAPSED_TERMINAL_LINES,
                overflow = TextOverflow.Ellipsis,
            )
        }
        if (block.truncated) EvidenceLimitNote()
        if (
            expanded && evidenceExceedsPreview(
                block.output,
                EXPANDED_EVIDENCE_LINES,
                EXPANDED_EVIDENCE_CHARACTERS,
            )
        ) {
            EvidencePreviewLimitNote()
        }
        if (canExpand) EvidenceToggle(expanded) { expanded = !expanded }
    }
}

@Composable
private fun FileView(block: FileBlock) {
    EvidenceFrame(
        tag = "FILE",
        title = block.label,
        state = block.state,
        detail = listOfNotNull(fileChangeLabel(block.change), block.language).joinToString(" · ").ifBlank { null },
    ) {
        block.detail?.takeIf(String::isNotBlank)?.let {
            Text(it, color = Muted, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun DiffView(block: DiffBlock) {
    var expanded by remember(block.id) { mutableStateOf(false) }
    val canExpand = evidenceNeedsExpansion(block.preview.orEmpty(), COLLAPSED_DIFF_LINES)
    val visiblePreview = evidencePreviewText(
        value = block.preview.orEmpty(),
        maxLines = if (expanded) EXPANDED_EVIDENCE_LINES else COLLAPSED_DIFF_LINES,
        maxCodePoints = if (expanded) EXPANDED_EVIDENCE_CHARACTERS else COLLAPSED_EVIDENCE_CHARACTERS,
    )
    EvidenceFrame(
        tag = "DIFF",
        title = block.fileLabel,
        state = StepState.COMPLETED,
        detail = block.summary,
    ) {
        if (block.additions != null || block.deletions != null) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                block.additions?.let { EvidenceCount("+$it", CompleteGreen, GreenSoft) }
                block.deletions?.let { EvidenceCount("-$it", RiskRed, RedSoft) }
            }
        }
        block.preview?.takeIf(String::isNotBlank)?.let {
            Text(
                text = visiblePreview,
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(12.dp),
                color = Ink,
                style = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace),
                maxLines = if (expanded) EXPANDED_EVIDENCE_LINES else COLLAPSED_DIFF_LINES,
                overflow = TextOverflow.Ellipsis,
            )
        }
        if (block.truncated) EvidenceLimitNote()
        if (
            expanded && evidenceExceedsPreview(
                block.preview.orEmpty(),
                EXPANDED_EVIDENCE_LINES,
                EXPANDED_EVIDENCE_CHARACTERS,
            )
        ) {
            EvidencePreviewLimitNote()
        }
        if (canExpand) EvidenceToggle(expanded) { expanded = !expanded }
    }
}

@Composable
private fun ToolView(block: ToolBlock) {
    var expanded by remember(block.id) { mutableStateOf(false) }
    EvidenceFrame(
        tag = "TOOL",
        title = block.label,
        state = block.state,
        detail = block.durationLabel,
    ) {
        block.detail?.takeIf(String::isNotBlank)?.let {
            if (expanded) {
                Text(
                    it,
                    color = Muted,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 6,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            EvidenceToggle(expanded) { expanded = !expanded }
        }
    }
}

@Composable
private fun TestView(block: TestBlock) {
    EvidenceFrame(
        tag = "TEST",
        title = block.title,
        state = block.state,
        detail = block.durationLabel,
    ) {
        Text(block.summary, color = Muted, style = MaterialTheme.typography.bodyMedium)
        if (block.passed != null || block.failed != null || block.skipped != null) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                block.passed?.let { EvidenceCount("通过 $it", CompleteGreen, GreenSoft) }
                block.failed?.let { EvidenceCount("失败 $it", RiskRed, RedSoft) }
                block.skipped?.let { EvidenceCount("跳过 $it", Muted, MaterialTheme.colorScheme.surfaceVariant) }
            }
        }
    }
}

@Composable
private fun EvidenceFrame(
    tag: String,
    title: String,
    state: StepState,
    detail: String?,
    content: @Composable () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .border(1.dp, Divider, RoundedCornerShape(10.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                text = tag,
                modifier = Modifier
                    .clip(RoundedCornerShape(6.dp))
                    .background(BlueSoft)
                    .padding(horizontal = 7.dp, vertical = 4.dp),
                color = SignalBlue,
                style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                fontWeight = FontWeight.Bold,
            )
            Spacer(Modifier.weight(1f))
            Text(stepLabel(state), color = stepColor(state), style = MaterialTheme.typography.labelMedium)
        }
        Text(
            title,
            color = Ink,
            style = MaterialTheme.typography.titleMedium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
        detail?.takeIf(String::isNotBlank)?.let {
            Text(it, color = Muted, style = MaterialTheme.typography.labelMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        content()
    }
}

@Composable
private fun EvidenceCount(label: String, color: Color, background: Color) {
    Text(
        text = label,
        modifier = Modifier
            .clip(RoundedCornerShape(7.dp))
            .background(background)
            .padding(horizontal = 8.dp, vertical = 5.dp),
        color = color,
        style = MaterialTheme.typography.labelMedium,
        fontWeight = FontWeight.SemiBold,
    )
}

@Composable
private fun EvidenceToggle(expanded: Boolean, onClick: () -> Unit) {
    TextButton(onClick = onClick, modifier = Modifier.heightIn(min = 48.dp)) {
        Text(if (expanded) "收起详情" else "展开详情")
    }
}

@Composable
private fun EvidenceLimitNote() {
    Text("内容已按安全上限截断", color = WarningAmber, style = MaterialTheme.typography.labelMedium)
}

@Composable
private fun EvidencePreviewLimitNote() {
    Text("预览最多显示 24 行", color = Muted, style = MaterialTheme.typography.labelMedium)
}

internal fun evidenceLineCount(value: String): Int = if (value.isEmpty()) 0 else value.count { it == '\n' } + 1

internal fun evidenceNeedsExpansion(value: String, collapsedLines: Int): Boolean =
    evidenceLineCount(value) > collapsedLines || value.length > COLLAPSED_EVIDENCE_CHARACTERS

internal fun evidencePreviewText(value: String, maxLines: Int, maxCodePoints: Int): String {
    if (value.isEmpty()) return value
    val lineBounded = value.lineSequence().take(maxLines).joinToString("\n")
    val codePointCount = lineBounded.codePointCount(0, lineBounded.length)
    val characterBounded = if (codePointCount > maxCodePoints) {
        lineBounded.substring(0, lineBounded.offsetByCodePoints(0, maxCodePoints))
    } else {
        lineBounded
    }
    return if (characterBounded == value) characterBounded else characterBounded.trimEnd() + " …"
}

internal fun evidenceExceedsPreview(value: String, maxLines: Int, maxCodePoints: Int): Boolean =
    evidenceLineCount(value) > maxLines || value.codePointCount(0, value.length) > maxCodePoints

private fun fileChangeLabel(change: FileChangeKind): String = when (change) {
    FileChangeKind.CREATED -> "新建"
    FileChangeKind.MODIFIED -> "修改"
    FileChangeKind.DELETED -> "删除"
    FileChangeKind.RENAMED -> "重命名"
    FileChangeKind.READ -> "读取"
    FileChangeKind.UNKNOWN -> ""
}

private const val COLLAPSED_TERMINAL_LINES = 4
private const val COLLAPSED_DIFF_LINES = 5
private const val EXPANDED_EVIDENCE_LINES = 24
private const val COLLAPSED_EVIDENCE_CHARACTERS = 480
private const val EXPANDED_EVIDENCE_CHARACTERS = 4_096
private const val MAX_MEDIA_EDGE = 12_000
private const val MAX_MEDIA_SOURCE_PIXELS = 64_000_000L
private const val MAX_DECODED_MEDIA_EDGE = 4_096
private const val MAX_DECODED_MEDIA_PIXELS = 16_000_000L
private const val MAX_USER_MEDIA_PREVIEW_EDGE = 768
private const val MAX_USER_MEDIA_PREVIEW_PIXELS = 600_000L
private const val MIN_MEDIA_ASPECT_RATIO = 0.30f
private const val MAX_MEDIA_ASPECT_RATIO = 3.0f

@Composable
private fun StatusView(block: StatusBlock) {
    val foreground by animateColorAsState(toneColor(block.tone), tween(260), label = "status-color")
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(toneBackground(block.tone))
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (block.tone == Tone.SUCCESS) {
                Icon(Icons.Default.Check, null, tint = foreground, modifier = Modifier.size(20.dp))
            } else if (block.tone == Tone.DANGER || block.tone == Tone.WARNING) {
                Icon(Icons.Default.Close, null, tint = foreground, modifier = Modifier.size(20.dp))
            } else {
                CircularProgressIndicator(
                    modifier = Modifier.size(18.dp),
                    strokeWidth = 2.dp,
                    color = foreground,
                    trackColor = foreground.copy(alpha = 0.16f),
                )
            }
            Spacer(Modifier.width(9.dp))
            Text(block.label, color = Ink, style = MaterialTheme.typography.titleMedium)
        }
        block.detail?.let { Text(it, color = Muted, style = MaterialTheme.typography.bodyMedium) }
        block.progress?.let {
            LinearProgressIndicator(
                progress = { it.coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth(),
                color = foreground,
                trackColor = foreground.copy(alpha = 0.14f),
            )
        }
    }
}

@Composable
private fun StepsView(block: StepsBlock) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(block.title, style = MaterialTheme.typography.titleMedium)
        block.items.forEach { item ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                StepIcon(item.state)
                Spacer(Modifier.width(10.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        item.label,
                        color = if (item.state == StepState.PENDING) Muted else Ink,
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = if (item.state == StepState.ACTIVE) FontWeight.SemiBold else FontWeight.Normal,
                    )
                    item.detail?.let { Text(it, color = Muted, style = MaterialTheme.typography.labelMedium) }
                }
                Text(
                    stepLabel(item.state),
                    color = stepColor(item.state),
                    style = MaterialTheme.typography.labelMedium,
                )
            }
        }
    }
}

@Composable
private fun StepIcon(state: StepState) {
    val color by animateColorAsState(stepColor(state), tween(260), label = "step-color")
    Box(
        modifier = Modifier
            .size(24.dp)
            .border(2.dp, color, CircleShape)
            .background(if (state == StepState.COMPLETED) color else Color.Transparent, CircleShape),
        contentAlignment = Alignment.Center,
    ) {
        when (state) {
            StepState.COMPLETED -> Icon(Icons.Default.Check, null, tint = Paper, modifier = Modifier.size(15.dp))
            StepState.FAILED -> Icon(Icons.Default.Close, null, tint = color, modifier = Modifier.size(14.dp))
            StepState.ACTIVE -> Box(Modifier.size(8.dp).background(color, CircleShape))
            StepState.PENDING -> Unit
        }
    }
}

@Composable
private fun MetricsView(block: MetricsBlock) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("结果摘要", style = MaterialTheme.typography.titleMedium)
        block.items.chunked(3).forEach { rowItems ->
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                rowItems.forEach { metric ->
                    Column(
                        modifier = Modifier
                            .weight(1f)
                            .clip(RoundedCornerShape(10.dp))
                            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.55f))
                            .padding(horizontal = 8.dp, vertical = 12.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Text(
                            text = metric.value,
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        Text(
                            text = listOfNotNull(metric.label, metric.unit).joinToString(" · "),
                            color = Muted,
                            style = MaterialTheme.typography.labelMedium,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
                repeat(max(0, 3 - rowItems.size)) { Spacer(Modifier.weight(1f)) }
            }
        }
    }
}

@Composable
private fun FactsView(block: FactsBlock) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .border(1.dp, Divider, RoundedCornerShape(10.dp)),
    ) {
        block.items.forEachIndexed { index, fact ->
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                verticalAlignment = Alignment.Top,
            ) {
                Text(fact.label, color = Muted, modifier = Modifier.width(76.dp), style = MaterialTheme.typography.bodyMedium)
                Column(modifier = Modifier.weight(1f)) {
                    Text(fact.value, color = Ink, style = MaterialTheme.typography.bodyMedium, fontWeight = FontWeight.Medium)
                    fact.note?.let { Text(it, color = Muted, style = MaterialTheme.typography.labelMedium) }
                }
            }
            if (index != block.items.lastIndex) Spacer(Modifier.fillMaxWidth().height(1.dp).background(Divider))
        }
    }
}

@Composable
private fun SourceView(block: SourceBlock) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(BlueSoft)
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(8.dp).background(SignalBlue, CircleShape))
        Spacer(Modifier.width(10.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(block.label, style = MaterialTheme.typography.labelLarge)
            Text(block.detail, color = Muted, style = MaterialTheme.typography.bodyMedium)
        }
        block.trustLabel?.let { Text(it, color = CompleteGreen, style = MaterialTheme.typography.labelMedium) }
    }
}

@Composable
private fun RelayView(block: RelayBlock) {
    val activeIndex = block.nodes.indexOfFirst { it.state == StepState.ACTIVE }
    val targetIndex = activeIndex.takeIf { it >= 0 }
        ?: block.nodes.indexOfLast { it.state == StepState.COMPLETED }.coerceAtLeast(0)
    val animatedIndex = remember { Animatable(targetIndex.toFloat()) }
    var previousTarget by remember { mutableFloatStateOf(targetIndex.toFloat()) }

    LaunchedEffect(targetIndex) {
        if (previousTarget != targetIndex.toFloat()) {
            animatedIndex.animateTo(targetIndex.toFloat(), tween(380))
            previousTarget = targetIndex.toFloat()
        } else {
            animatedIndex.snapTo(targetIndex.toFloat())
        }
    }

    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(block.title, style = MaterialTheme.typography.titleMedium)
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(IntrinsicSize.Min),
        ) {
            RelayRail(
                states = block.nodes.map { it.state },
                animatedIndex = animatedIndex.value.takeIf { activeIndex >= 0 },
                modifier = Modifier
                    .width(30.dp)
                    .fillMaxHeight(),
            )
            Spacer(Modifier.width(8.dp))
            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(0.dp),
            ) {
                block.nodes.forEach { node ->
                    Column(
                        modifier = Modifier.heightIn(min = 50.dp),
                        verticalArrangement = Arrangement.Center,
                    ) {
                        Text(
                            node.label,
                            color = if (node.state == StepState.PENDING) Muted else Ink,
                            style = MaterialTheme.typography.bodyMedium,
                            fontWeight = if (node.state == StepState.ACTIVE) FontWeight.Bold else FontWeight.Medium,
                        )
                        node.detail?.let { Text(it, color = stepColor(node.state), style = MaterialTheme.typography.labelMedium) }
                    }
                }
            }
        }
    }
}

@Composable
private fun RelayRail(
    states: List<StepState>,
    animatedIndex: Float?,
    modifier: Modifier,
) {
    val nodeGapPx = with(LocalDensity.current) { 50.dp.toPx() }
    Canvas(modifier = modifier) {
        if (states.isEmpty()) return@Canvas
        val x = size.width / 2f
        val firstY = nodeGapPx / 2f
        val lastY = firstY + nodeGapPx * (states.size - 1)
        drawLine(Divider, Offset(x, firstY), Offset(x, lastY), strokeWidth = 4f, cap = StrokeCap.Round)

        states.forEachIndexed { index, state ->
            val y = firstY + index * nodeGapPx
            val color = stepColor(state)
            drawCircle(Paper, radius = 9f, center = Offset(x, y))
            drawCircle(color, radius = 8f, center = Offset(x, y), style = Stroke(width = 4f))
            if (state == StepState.COMPLETED) drawCircle(color, radius = 5f, center = Offset(x, y))
        }

        animatedIndex?.let { activePosition ->
            val pulseY = firstY + activePosition
                .coerceIn(0f, (states.size - 1).coerceAtLeast(0).toFloat()) * nodeGapPx
            drawCircle(SignalBlue.copy(alpha = 0.18f), radius = 14f, center = Offset(x, pulseY))
            drawCircle(SignalBlue, radius = 5.5f, center = Offset(x, pulseY))
        }
    }
}

@Composable
private fun NoticeView(block: NoticeBlock) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(toneBackground(block.tone))
            .padding(14.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Icon(
            if (block.tone == Tone.SUCCESS) Icons.Default.Check else Icons.Default.Close,
            contentDescription = null,
            tint = toneColor(block.tone),
            modifier = Modifier.size(20.dp),
        )
        Spacer(Modifier.width(10.dp))
        Column {
            Text(block.title, style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(3.dp))
            Text(block.body, color = Muted, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun ActionsView(
    block: ActionsBlock,
    onAction: (String) -> Unit,
    actionRetryNotBeforeMillis: Map<String, Long>,
) {
    val retryAt = actionBlockRetryNotBeforeMillis(block, actionRetryNotBeforeMillis)
    var now by remember(retryAt) { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(retryAt) {
        while (now < retryAt) {
            delay(minOf(1_000, retryAt - now))
            now = System.currentTimeMillis()
        }
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (retryAt > now) {
            val seconds = ((retryAt - now + 999) / 1_000).coerceAtLeast(1)
            Text("请等待 ${seconds} 秒后再操作", color = WarningAmber)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            block.items.forEach { action ->
                ActionButton(
                    action,
                    onAction,
                    isActionBlockRateLimitElapsed(block, actionRetryNotBeforeMillis, now),
                )
            }
        }
    }
}

internal fun actionBlockRetryNotBeforeMillis(
    block: ActionsBlock,
    transientDeadlines: Map<String, Long>,
): Long = block.items.maxOfOrNull { action ->
    maxOf(action.retryNotBeforeMillis, transientDeadlines[action.actionRef] ?: 0)
} ?: 0

internal fun isActionBlockRateLimitElapsed(
    block: ActionsBlock,
    transientDeadlines: Map<String, Long>,
    nowMillis: Long,
): Boolean = nowMillis >= actionBlockRetryNotBeforeMillis(block, transientDeadlines)

@Composable
private fun ActionButton(
    action: ActionItem,
    onAction: (String) -> Unit,
    rateLimitElapsed: Boolean,
) {
    val content: @Composable () -> Unit = {
        if (action.label.contains("重试")) {
            Icon(Icons.Default.Refresh, null, modifier = Modifier.size(18.dp))
            Spacer(Modifier.width(6.dp))
        }
        Text(action.label)
    }
    if (action.kind == ActionKind.PRIMARY) {
        Button(
            onClick = { onAction(action.actionRef) },
            enabled = action.enabled && rateLimitElapsed,
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 10.dp),
            content = { content() },
        )
    } else {
        OutlinedButton(
            onClick = { onAction(action.actionRef) },
            enabled = action.enabled && rateLimitElapsed,
            colors = ButtonDefaults.outlinedButtonColors(
                contentColor = if (action.kind == ActionKind.DESTRUCTIVE) RiskRed else SignalBlue,
            ),
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 10.dp),
            content = { content() },
        )
    }
}

@Composable
private fun UnknownView(block: UnknownBlock) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .border(1.dp, Divider, RoundedCornerShape(10.dp))
            .padding(12.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Icon(Icons.Default.Close, null, tint = Muted, modifier = Modifier.size(18.dp))
        Spacer(Modifier.width(8.dp))
        Text(block.readableText(), color = Muted, style = MaterialTheme.typography.bodyMedium)
    }
}

private fun stepLabel(state: StepState): String = when (state) {
    StepState.PENDING -> "等待"
    StepState.ACTIVE -> "执行中"
    StepState.COMPLETED -> "完成"
    StepState.FAILED -> "失败"
}

private fun stepColor(state: StepState): Color = when (state) {
    StepState.PENDING -> Muted.copy(alpha = 0.55f)
    StepState.ACTIVE -> SignalBlue
    StepState.COMPLETED -> CompleteGreen
    StepState.FAILED -> RiskRed
}

private fun toneColor(tone: Tone): Color = when (tone) {
    Tone.NEUTRAL -> Muted
    Tone.INFO -> SignalBlue
    Tone.SUCCESS -> CompleteGreen
    Tone.WARNING -> WarningAmber
    Tone.DANGER -> RiskRed
}

@Composable
private fun toneBackground(tone: Tone): Color = when (tone) {
    Tone.NEUTRAL -> MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.7f)
    Tone.INFO -> BlueSoft
    Tone.SUCCESS -> GreenSoft
    Tone.WARNING -> AmberSoft
    Tone.DANGER -> RedSoft
}
