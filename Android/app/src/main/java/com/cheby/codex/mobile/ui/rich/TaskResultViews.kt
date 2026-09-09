package com.cheby.codex.mobile.ui.rich

import android.graphics.Bitmap
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.cheby.codex.mobile.model.BilingualCardBlock
import com.cheby.codex.mobile.model.ComparisonBlock
import com.cheby.codex.mobile.model.ComparisonItem
import com.cheby.codex.mobile.model.ReferenceGalleryBlock
import com.cheby.codex.mobile.model.ReferenceImageItem
import com.cheby.codex.mobile.ui.theme.CompleteGreen
import com.cheby.codex.mobile.ui.theme.Divider
import com.cheby.codex.mobile.ui.theme.GreenSoft
import com.cheby.codex.mobile.ui.theme.Ink
import com.cheby.codex.mobile.ui.theme.Muted
import com.cheby.codex.mobile.ui.theme.Paper
import com.cheby.codex.mobile.ui.theme.SignalBlue
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

private val PassportBlue = Color(0xFF173A64)
private val HandoffRed = Color(0xFFB4423D)
private val Frost = Color(0xFFF4F7FA)

@Composable
internal fun ReferenceGalleryView(block: ReferenceGalleryBlock) {
    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        Text(block.title, color = Ink, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
        if (block.subtitle.isNotBlank()) {
            Text(block.subtitle, color = Muted, style = MaterialTheme.typography.bodyMedium)
        }
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            block.items.forEach { item -> GalleryItemCard(item) }
        }
    }
}

@Composable
private fun GalleryItemCard(item: ReferenceImageItem) {
    Card(
        modifier = Modifier.width(238.dp),
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = Frost),
        border = androidx.compose.foundation.BorderStroke(1.dp, Divider),
    ) {
        ReferenceImageTile(
            query = item.imageQuery,
            label = item.imageLabel,
            modifier = Modifier
                .fillMaxWidth()
                .aspectRatio(1.45f),
        )
        Column(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(3.dp),
        ) {
            Text(
                item.title,
                color = Ink,
                fontWeight = FontWeight.Bold,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            if (item.caption.isNotBlank()) {
                Text(
                    item.caption,
                    color = Muted,
                    style = MaterialTheme.typography.bodySmall,
                    maxLines = 3,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            if (item.uncertain) {
                Text("内容需核对", color = HandoffRed, style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}

@Composable
internal fun ComparisonView(block: ComparisonBlock) {
    var opened by rememberSaveable(block.id) { mutableStateOf(false) }
    val recommendation = block.items.firstOrNull(ComparisonItem::recommended)
    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(modifier = Modifier.weight(1f)) {
                Text(block.title, color = Ink, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Text("${block.items.size} 个候选", color = Muted, style = MaterialTheme.typography.labelMedium)
            }
            if (recommendation != null) {
                Surface(color = GreenSoft, shape = RoundedCornerShape(999.dp)) {
                    Text(
                        "建议先看 ${recommendation.primary}",
                        color = CompleteGreen,
                        style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.padding(horizontal = 9.dp, vertical = 5.dp),
                    )
                }
            }
        }
        Text(block.subtitle, color = Muted, style = MaterialTheme.typography.bodyMedium)
        Button(
            onClick = { opened = true },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("comparison_open"),
            colors = ButtonDefaults.buttonColors(containerColor = SignalBlue),
        ) {
            Text("打开比较")
        }
    }
    if (opened) ComparisonDialog(block = block, onClose = { opened = false })
}

@Composable
private fun ComparisonDialog(block: ComparisonBlock, onClose: () -> Unit) {
    var selectedId by rememberSaveable(block.id) { mutableStateOf("") }
    val selected = block.items.firstOrNull { it.id == selectedId }
    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(
            modifier = Modifier
                .fillMaxSize()
                .testTag("comparison_dialog"),
            color = Frost,
        ) {
            Column(Modifier.fillMaxSize()) {
                ResultDialogHeader(
                    title = block.title,
                    subtitle = selected?.let { "已选：${it.primary}" } ?: block.subtitle,
                    onClose = onClose,
                )
                LazyColumn(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    items(block.items, key = ComparisonItem::id) { item ->
                        ComparisonItemCard(
                            item = item,
                            selected = item.id == selectedId,
                            selectLabel = block.selectLabel,
                            onSelect = { selectedId = if (selectedId == item.id) "" else item.id },
                        )
                    }
                }
                if (selected != null) {
                    Surface(color = Paper, shadowElevation = 9.dp) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text("当前选择", color = Muted, style = MaterialTheme.typography.labelMedium)
                                Text(selected.primary, color = Ink, fontWeight = FontWeight.Bold)
                            }
                            OutlinedButton(onClick = { selectedId = "" }) { Text("取消选择") }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun ComparisonItemCard(
    item: ComparisonItem,
    selected: Boolean,
    selectLabel: String,
    onSelect: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onSelect),
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = Paper),
        border = androidx.compose.foundation.BorderStroke(
            if (selected) 2.dp else 1.dp,
            if (selected) SignalBlue else Divider,
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = if (selected) 3.dp else 0.dp),
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (item.imageQuery.isNotBlank()) {
                ReferenceImageTile(
                    query = item.imageQuery,
                    label = item.imageLabel,
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 132.dp, max = 180.dp),
                )
            }
            Column(
                modifier = Modifier.padding(
                    start = 14.dp,
                    end = 14.dp,
                    top = if (item.imageQuery.isBlank()) 14.dp else 0.dp,
                    bottom = 14.dp,
                ),
                verticalArrangement = Arrangement.spacedBy(7.dp),
            ) {
                Row(verticalAlignment = Alignment.Top) {
                    Column(Modifier.weight(1f)) {
                        Text(item.primary, color = Ink, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                        if (item.secondary.isNotBlank()) {
                            Text(item.secondary, color = SignalBlue, style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                    if (item.recommended || item.badge.isNotBlank()) {
                        Surface(
                            color = if (item.recommended) GreenSoft else Frost,
                            shape = RoundedCornerShape(999.dp),
                        ) {
                            Text(
                                if (item.recommended) "推荐" else item.badge,
                                color = if (item.recommended) CompleteGreen else PassportBlue,
                                style = MaterialTheme.typography.labelSmall,
                                modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
                            )
                        }
                    }
                }
                if (item.detail.isNotBlank()) {
                    Text(item.detail, color = Muted, style = MaterialTheme.typography.bodyMedium)
                }
                item.facts.chunked(2).forEach { rowFacts ->
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        rowFacts.forEach { fact ->
                            Column(
                                modifier = Modifier
                                    .weight(1f)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(Frost)
                                    .padding(9.dp),
                            ) {
                                Text(fact.label, color = Muted, style = MaterialTheme.typography.labelSmall)
                                Text(fact.value, color = Ink, fontWeight = FontWeight.Bold)
                            }
                        }
                        if (rowFacts.size == 1) Spacer(Modifier.weight(1f))
                    }
                }
                if (item.uncertain) {
                    Text("部分信息未核实", color = HandoffRed, style = MaterialTheme.typography.labelMedium)
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        if (selected) "已选择" else selectLabel,
                        color = if (selected) CompleteGreen else SignalBlue,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f),
                    )
                    if (selected) {
                        Icon(Icons.Default.Check, contentDescription = null, tint = CompleteGreen)
                    }
                }
            }
        }
    }
}

@Composable
internal fun BilingualCardView(block: BilingualCardBlock) {
    var opened by rememberSaveable(block.id) { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .width(5.dp)
                    .height(42.dp)
                    .clip(RoundedCornerShape(999.dp))
                    .background(PassportBlue),
            )
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(block.titleOriginal, color = Ink, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                Text(block.titleChinese, color = Muted, style = MaterialTheme.typography.bodyMedium)
            }
            Surface(color = Frost, shape = RoundedCornerShape(999.dp)) {
                Text(
                    "双语",
                    color = PassportBlue,
                    style = MaterialTheme.typography.labelMedium,
                    modifier = Modifier.padding(horizontal = 9.dp, vertical = 5.dp),
                )
            }
        }
        Button(
            onClick = { opened = true },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("bilingual_open"),
            colors = ButtonDefaults.buttonColors(containerColor = PassportBlue),
        ) {
            Text(block.showLabel)
        }
    }
    if (opened) BilingualDialog(block = block, onClose = { opened = false })
}

@Composable
private fun BilingualDialog(block: BilingualCardBlock, onClose: () -> Unit) {
    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(
            modifier = Modifier
                .fillMaxSize()
                .testTag("bilingual_dialog"),
            color = Color(0xFFF8FAFC),
        ) {
            Column(Modifier.fillMaxSize()) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(PassportBlue)
                        .padding(start = 18.dp, end = 8.dp, top = 16.dp, bottom = 16.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(
                            block.titleOriginal,
                            color = Paper,
                            style = MaterialTheme.typography.headlineSmall,
                            fontWeight = FontWeight.Bold,
                        )
                        Text(block.titleChinese, color = Paper.copy(alpha = 0.72f))
                    }
                    IconButton(onClick = onClose) {
                        Icon(Icons.Default.Close, contentDescription = "关闭", tint = Paper)
                    }
                }
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(4.dp)
                        .background(HandoffRed),
                )
                LazyColumn(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(18.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp),
                ) {
                    if (block.contextOriginal.isNotBlank() || block.contextChinese.isNotBlank()) {
                        item {
                            Card(
                                colors = CardDefaults.cardColors(containerColor = Frost),
                                shape = RoundedCornerShape(14.dp),
                            ) {
                                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                                    if (block.contextOriginal.isNotBlank()) {
                                        Text(block.contextOriginal, color = PassportBlue, fontWeight = FontWeight.Bold)
                                    }
                                    if (block.contextChinese.isNotBlank()) {
                                        Text(block.contextChinese, color = Muted)
                                    }
                                }
                            }
                        }
                    }
                    items(block.lines, key = { it.id }) { line ->
                        Row(verticalAlignment = Alignment.Top) {
                            Box(
                                modifier = Modifier
                                    .padding(top = 8.dp)
                                    .size(8.dp)
                                    .clip(CircleShape)
                                    .background(HandoffRed),
                            )
                            Spacer(Modifier.width(12.dp))
                            Column(
                                modifier = Modifier
                                    .weight(1f)
                                    .border(1.dp, Divider, RoundedCornerShape(14.dp))
                                    .background(Paper, RoundedCornerShape(14.dp))
                                    .padding(14.dp),
                                verticalArrangement = Arrangement.spacedBy(6.dp),
                            ) {
                                Text(
                                    line.original,
                                    color = Ink,
                                    style = MaterialTheme.typography.titleMedium,
                                    fontWeight = FontWeight.Bold,
                                )
                                Text(line.translation, color = SignalBlue, style = MaterialTheme.typography.bodyMedium)
                                if (line.note.isNotBlank()) {
                                    Text(line.note, color = Muted, style = MaterialTheme.typography.bodySmall)
                                }
                            }
                        }
                    }
                }
                Surface(color = Paper, shadowElevation = 9.dp) {
                    Button(
                        onClick = onClose,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(16.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = CompleteGreen),
                    ) {
                        Text("完成展示")
                    }
                }
            }
        }
    }
}

@Composable
private fun ResultDialogHeader(title: String, subtitle: String, onClose: () -> Unit) {
    Surface(color = Paper, shadowElevation = 4.dp) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(start = 16.dp, end = 8.dp, top = 13.dp, bottom = 13.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(title, color = Ink, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                Text(
                    subtitle,
                    color = Muted,
                    style = MaterialTheme.typography.bodySmall,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
            IconButton(onClick = onClose) {
                Icon(Icons.Default.Close, contentDescription = "关闭")
            }
        }
    }
}

@Composable
private fun ReferenceImageTile(query: String, label: String, modifier: Modifier = Modifier) {
    var bitmap by remember(query) { mutableStateOf<Bitmap?>(null) }
    LaunchedEffect(query) {
        bitmap = withContext(Dispatchers.IO) { ReferenceImageLoader.load(query) }
    }
    Box(
        modifier = modifier
            .background(Color(0xFFE7ECF1)),
    ) {
        if (bitmap != null) {
            Image(
                bitmap = requireNotNull(bitmap).asImageBitmap(),
                contentDescription = label.ifBlank { "参考图片" },
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            ReferencePlaceholder(Modifier.fillMaxSize())
        }
        Text(
            text = if (bitmap != null) label.ifBlank { "参考图片" } else "图片待补充",
            color = Paper,
            style = MaterialTheme.typography.labelSmall,
            maxLines = 2,
            modifier = Modifier
                .align(Alignment.BottomStart)
                .fillMaxWidth()
                .background(Ink.copy(alpha = 0.74f))
                .padding(horizontal = 7.dp, vertical = 4.dp),
        )
    }
}

@Composable
private fun ReferencePlaceholder(modifier: Modifier = Modifier) {
    Canvas(modifier) {
        val margin = size.minDimension * 0.2f
        drawLine(
            color = Color(0xFF8EA0B3),
            start = Offset(margin, size.height - margin),
            end = Offset(size.width * 0.48f, size.height * 0.46f),
            strokeWidth = 6f,
        )
        drawLine(
            color = Color(0xFF8EA0B3),
            start = Offset(size.width * 0.48f, size.height * 0.46f),
            end = Offset(size.width - margin, size.height - margin),
            strokeWidth = 6f,
        )
        drawCircle(
            color = Color(0xFFB8C5D1),
            radius = size.minDimension * 0.08f,
            center = Offset(size.width * 0.72f, size.height * 0.28f),
        )
    }
}
