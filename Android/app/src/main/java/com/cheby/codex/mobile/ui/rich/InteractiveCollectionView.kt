package com.cheby.codex.mobile.ui.rich

import android.graphics.Bitmap
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
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
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.cheby.codex.mobile.model.InteractiveCollectionBlock
import com.cheby.codex.mobile.model.InteractiveCollectionItem
import com.cheby.codex.mobile.model.InteractiveImageKind
import com.cheby.codex.mobile.ui.theme.CompleteGreen
import com.cheby.codex.mobile.ui.theme.Divider
import com.cheby.codex.mobile.ui.theme.GreenSoft
import com.cheby.codex.mobile.ui.theme.Ink
import com.cheby.codex.mobile.ui.theme.Muted
import com.cheby.codex.mobile.ui.theme.Paper
import com.cheby.codex.mobile.ui.theme.SignalBlue
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Browse/select/review/present UI for a business-agnostic rich collection. */
@Composable
internal fun InteractiveCollectionView(block: InteractiveCollectionBlock) {
    var opened by rememberSaveable(block.id) { mutableStateOf(false) }
    val itemCount = block.groups.sumOf { it.items.size }
    val groupCount = block.groups.size

    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(block.title, color = Ink, style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(3.dp))
                Text(
                    "$groupCount 个分类 · $itemCount 个可选项",
                    color = Muted,
                    style = MaterialTheme.typography.labelMedium,
                )
            }
            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(999.dp))
                    .background(GreenSoft)
                    .padding(horizontal = 10.dp, vertical = 5.dp),
            ) {
                Text("可点选", color = CompleteGreen, style = MaterialTheme.typography.labelMedium)
            }
        }
        Text(block.subtitle, color = Muted, style = MaterialTheme.typography.bodyMedium)
        Button(
            onClick = { opened = true },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("interactive_collection_open"),
            colors = ButtonDefaults.buttonColors(containerColor = SignalBlue),
        ) {
            Text("打开完整内容")
        }
    }

    if (opened) {
        InteractiveCollectionDialog(block = block, onClose = { opened = false })
    }
}

@Composable
private fun InteractiveCollectionDialog(
    block: InteractiveCollectionBlock,
    onClose: () -> Unit,
) {
    var encodedQuantities by rememberSaveable(block.id) { mutableStateOf("") }
    var selectedOnly by rememberSaveable(block.id) { mutableStateOf(false) }
    var presenting by rememberSaveable(block.id) { mutableStateOf(false) }
    val quantities = decodeQuantities(encodedQuantities)
    val allItems = block.groups.flatMap { it.items }
    val selected = allItems.mapNotNull { item ->
        quantities[item.id]?.takeIf { it > 0 }?.let { item to it }
    }
    val selectedCount = selected.sumOf { it.second }
    val totalMinor = selected.sumOf { (item, quantity) ->
        item.unitPriceMinor.takeIf { it >= 0 }?.times(quantity.toLong()) ?: 0L
    }
    val totalIsEstimated = selected.any { (item, _) -> item.unitPriceMinor < 0 || item.uncertain }
    val selectedQuantity = "$selectedCount ${block.quantityUnit}".trim()

    fun setQuantity(itemId: String, quantity: Int) {
        val next = quantities.toMutableMap()
        if (quantity <= 0) next.remove(itemId) else next[itemId] = quantity.coerceAtMost(99)
        encodedQuantities = encodeQuantities(next)
    }

    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(
            modifier = Modifier
                .fillMaxSize()
                .testTag("interactive_collection_dialog"),
            color = Color(0xFFF7F6F2),
        ) {
            if (presenting) {
                PresentationCard(
                    block = block,
                    selected = selected,
                    totalMinor = totalMinor,
                    onBack = { presenting = false },
                    onClose = onClose,
                )
            } else {
                Column(modifier = Modifier.fillMaxSize()) {
                    CollectionHeader(
                        title = if (selectedOnly) "已选内容" else block.title,
                        subtitle = if (selectedOnly) "$selectedQuantity，确认前仍可修改" else block.subtitle,
                        onClose = onClose,
                    )
                    LazyColumn(
                        modifier = Modifier
                            .weight(1f)
                            .fillMaxWidth(),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(
                            start = 14.dp,
                            end = 14.dp,
                            top = 8.dp,
                            bottom = 16.dp,
                        ),
                    ) {
                        if (selectedOnly) {
                            if (selected.isEmpty()) {
                                item {
                                    EmptySelection()
                                }
                            } else {
                                items(selected, key = { it.first.id }) { (item, quantity) ->
                                    CollectionItemCard(
                                        item = item,
                                        quantity = quantity,
                                        currencyLabel = block.currencyLabel,
                                        minorUnitScale = block.minorUnitScale,
                                        onQuantityChange = { setQuantity(item.id, it) },
                                    )
                                }
                            }
                        } else {
                            block.groups.forEach { group ->
                                item(key = "group-${group.id}") {
                                    Text(
                                        group.title,
                                        color = Ink,
                                        style = MaterialTheme.typography.titleMedium,
                                        fontWeight = FontWeight.Bold,
                                        modifier = Modifier.padding(top = 8.dp, bottom = 2.dp),
                                    )
                                }
                                items(group.items, key = { it.id }) { item ->
                                    CollectionItemCard(
                                        item = item,
                                        quantity = quantities[item.id] ?: 0,
                                        currencyLabel = block.currencyLabel,
                                        minorUnitScale = block.minorUnitScale,
                                        onQuantityChange = { setQuantity(item.id, it) },
                                    )
                                }
                            }
                        }
                    }
                    SelectionTicket(
                        selectedCount = selectedCount,
                        total = formatMoney(totalMinor, block.minorUnitScale, block.currencyLabel),
                        totalIsEstimated = totalIsEstimated,
                        quantityUnit = block.quantityUnit,
                        selectedOnly = selectedOnly,
                        confirmLabel = block.confirmLabel,
                        onToggleSelected = { selectedOnly = !selectedOnly },
                        onConfirm = { if (selected.isNotEmpty()) presenting = true },
                    )
                }
            }
        }
    }
}

@Composable
private fun CollectionHeader(
    title: String,
    subtitle: String,
    onClose: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(Paper)
            .border(width = 0.dp, color = Divider)
            .padding(start = 18.dp, top = 12.dp, end = 8.dp, bottom = 12.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Text(title, color = Ink, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(4.dp))
            Text(subtitle, color = Muted, style = MaterialTheme.typography.bodySmall)
        }
        IconButton(onClick = onClose) {
            Icon(Icons.Default.Close, contentDescription = "关闭")
        }
    }
}

@Composable
private fun CollectionItemCard(
    item: InteractiveCollectionItem,
    quantity: Int,
    currencyLabel: String,
    minorUnitScale: Int,
    onQuantityChange: (Int) -> Unit,
) {
    val selected = quantity > 0
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("interactive_item_${item.id}")
            .clickable { onQuantityChange(if (selected) 0 else 1) },
        shape = RoundedCornerShape(14.dp),
        colors = CardDefaults.cardColors(containerColor = Paper),
        border = androidx.compose.foundation.BorderStroke(
            if (selected) 2.dp else 1.dp,
            if (selected) SignalBlue else Divider,
        ),
        elevation = CardDefaults.cardElevation(defaultElevation = if (selected) 3.dp else 0.dp),
    ) {
        Row(
            modifier = Modifier.padding(10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (item.imageKind != InteractiveImageKind.NONE && item.imageQuery.isNotBlank()) {
                ReferenceSelectionImage(item = item, modifier = Modifier.size(width = 108.dp, height = 104.dp))
                Spacer(Modifier.width(12.dp))
            }
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    item.primary,
                    color = Ink,
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Bold,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    item.secondary,
                    color = SignalBlue,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                if (item.detail.isNotBlank()) {
                    Text(
                        item.detail,
                        color = Muted,
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                Spacer(Modifier.height(7.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        displayItemPrice(item, minorUnitScale, currencyLabel),
                        color = Ink,
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f),
                    )
                    if (quantity == 0) {
                        Box(
                            modifier = Modifier
                                .clip(CircleShape)
                                .background(SignalBlue)
                                .size(34.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            Icon(Icons.Default.Add, contentDescription = "选择", tint = Paper)
                        }
                    } else {
                        QuantityStepper(quantity, onQuantityChange)
                    }
                }
                if (item.uncertain) {
                    Text("原图文字不清，已标记", color = Color(0xFF9A5B00), style = MaterialTheme.typography.labelSmall)
                }
            }
        }
    }
}

@Composable
private fun QuantityStepper(quantity: Int, onQuantityChange: (Int) -> Unit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(Color(0xFFE8F0FF)),
    ) {
        IconButton(onClick = { onQuantityChange(quantity - 1) }, modifier = Modifier.size(34.dp)) {
            Text("−", color = SignalBlue, style = MaterialTheme.typography.titleLarge)
        }
        Text(quantity.toString(), color = Ink, fontWeight = FontWeight.Bold)
        IconButton(onClick = { onQuantityChange(quantity + 1) }, modifier = Modifier.size(34.dp)) {
            Icon(Icons.Default.Add, contentDescription = "增加数量", tint = SignalBlue)
        }
    }
}

@Composable
private fun SelectionTicket(
    selectedCount: Int,
    total: String,
    totalIsEstimated: Boolean,
    quantityUnit: String,
    selectedOnly: Boolean,
    confirmLabel: String,
    onToggleSelected: () -> Unit,
    onConfirm: () -> Unit,
) {
    Surface(shadowElevation = 10.dp, color = Paper) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 14.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(
                modifier = Modifier
                    .weight(1f)
                    .clickable(onClick = onToggleSelected)
                    .padding(vertical = 4.dp),
            ) {
                Text(
                    if (selectedOnly) "查看全部" else "已选 ${"$selectedCount $quantityUnit".trim()}",
                    color = SignalBlue,
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    if (selectedCount == 0) {
                        "请选择内容"
                    } else {
                        "${if (totalIsEstimated) "预估合计" else "合计"} $total"
                    },
                    color = Ink,
                )
            }
            Button(
                onClick = onConfirm,
                enabled = selectedCount > 0,
                modifier = Modifier.testTag("interactive_confirm"),
                colors = ButtonDefaults.buttonColors(containerColor = CompleteGreen),
            ) {
                Icon(Icons.Default.Check, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(6.dp))
                Text(confirmLabel.ifBlank { "确认选择" })
            }
        }
    }
}

@Composable
private fun PresentationCard(
    block: InteractiveCollectionBlock,
    selected: List<Pair<InteractiveCollectionItem, Int>>,
    totalMinor: Long,
    onBack: () -> Unit,
    onClose: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(Color(0xFFF1F7F2))
            .padding(18.dp)
            .testTag("interactive_presentation_card"),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(44.dp)
                    .clip(CircleShape)
                    .background(CompleteGreen),
                contentAlignment = Alignment.Center,
            ) {
                Icon(Icons.Default.Check, contentDescription = null, tint = Paper)
            }
            Spacer(Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    block.presentationTitle.ifBlank { "已选内容" },
                    color = Ink,
                    style = MaterialTheme.typography.headlineSmall,
                    fontWeight = FontWeight.Bold,
                )
                if (block.presentationSubtitle.isNotBlank()) {
                    Text(
                        block.presentationSubtitle,
                        color = CompleteGreen,
                        style = MaterialTheme.typography.bodyLarge,
                    )
                }
            }
            IconButton(onClick = onClose) {
                Icon(Icons.Default.Close, contentDescription = "关闭")
            }
        }
        Spacer(Modifier.height(20.dp))
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f),
            colors = CardDefaults.cardColors(containerColor = Paper),
            shape = RoundedCornerShape(18.dp),
        ) {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(18.dp),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                items(selected, key = { it.first.id }) { (item, quantity) ->
                    Row(verticalAlignment = Alignment.Top) {
                        Text(
                            "$quantity×",
                            color = CompleteGreen,
                            fontWeight = FontWeight.Bold,
                            style = MaterialTheme.typography.titleMedium,
                            modifier = Modifier.width(44.dp),
                        )
                        Column(modifier = Modifier.weight(1f)) {
                            Text(item.primary, color = Ink, style = MaterialTheme.typography.titleMedium)
                            Text(item.secondary, color = Muted, style = MaterialTheme.typography.bodySmall)
                        }
                        Text(
                            displayItemPrice(item, block.minorUnitScale, block.currencyLabel),
                            color = Ink,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                }
                item {
                    Spacer(Modifier.height(4.dp))
                    Box(Modifier.fillMaxWidth().height(1.dp).background(Divider))
                    Spacer(Modifier.height(8.dp))
                    Row {
                        Text(
                            block.presentationTotalLabel,
                            color = Ink,
                            style = MaterialTheme.typography.titleLarge,
                            modifier = Modifier.weight(1f),
                        )
                        Text(
                            formatMoney(totalMinor, block.minorUnitScale, block.currencyLabel),
                            color = CompleteGreen,
                            style = MaterialTheme.typography.titleLarge,
                            fontWeight = FontWeight.Bold,
                        )
                    }
                    if (block.presentationNote.isNotBlank()) {
                        Spacer(Modifier.height(12.dp))
                        Text(block.presentationNote, color = Ink, style = MaterialTheme.typography.bodyLarge)
                    }
                }
            }
        }
        Spacer(Modifier.height(14.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedButton(onClick = onBack, modifier = Modifier.weight(1f)) {
                Text("返回修改")
            }
            Button(
                onClick = onClose,
                modifier = Modifier.weight(1f),
                colors = ButtonDefaults.buttonColors(containerColor = CompleteGreen),
            ) {
                Text("完成")
            }
        }
    }
}

@Composable
private fun EmptySelection() {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 48.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("还没有选择", color = Ink, style = MaterialTheme.typography.titleMedium)
        Text("点下面的“查看全部”继续选择", color = Muted)
    }
}

@Composable
private fun ReferenceSelectionImage(item: InteractiveCollectionItem, modifier: Modifier = Modifier) {
    var bitmap by remember(item.imageQuery, item.imageKind) { mutableStateOf<Bitmap?>(null) }
    LaunchedEffect(item.imageQuery, item.imageKind) {
        bitmap = if (
            item.imageKind == InteractiveImageKind.REPRESENTATIVE && item.imageQuery.isNotBlank()
        ) {
            withContext(Dispatchers.IO) { ReferenceImageLoader.load(item.imageQuery) }
        } else {
            null
        }
    }
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(10.dp))
            .background(Color(0xFFF0ECE3)),
    ) {
        if (bitmap != null) {
            Image(
                bitmap = requireNotNull(bitmap).asImageBitmap(),
                contentDescription = item.imageLabel.ifBlank { "参考图片" },
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            SelectionPlaceholder(Modifier.fillMaxSize())
        }
        Text(
            text = if (bitmap != null) {
                item.imageLabel.ifBlank { "参考图片，不代表实际对象" }
            } else {
                "图片待补充"
            },
            color = Paper,
            style = MaterialTheme.typography.labelSmall,
            maxLines = 2,
            modifier = Modifier
                .align(Alignment.BottomStart)
                .fillMaxWidth()
                .background(Ink.copy(alpha = 0.72f))
                .padding(horizontal = 5.dp, vertical = 3.dp),
        )
    }
}

@Composable
private fun SelectionPlaceholder(modifier: Modifier = Modifier) {
    Canvas(modifier) {
        val radius = size.minDimension * 0.29f
        drawCircle(Color(0xFFD8CDBB), radius = radius * 1.35f, center = center)
        drawCircle(Color(0xFFF9F7F0), radius = radius, center = center)
        drawArc(
            color = Color(0xFF6A8B65),
            startAngle = 205f,
            sweepAngle = 105f,
            useCenter = false,
            topLeft = Offset(center.x - radius * 0.65f, center.y - radius * 0.45f),
            size = Size(radius * 1.3f, radius * 0.9f),
            style = Stroke(width = 5f),
        )
    }
}

private fun encodeQuantities(values: Map<String, Int>): String = values.entries
    .filter { it.value > 0 }
    .sortedBy(Map.Entry<String, Int>::key)
    .joinToString(";") { "${it.key}=${it.value}" }

private fun decodeQuantities(encoded: String): Map<String, Int> = encoded
    .split(';')
    .mapNotNull { token ->
        val separator = token.lastIndexOf('=')
        if (separator <= 0) return@mapNotNull null
        val value = token.substring(separator + 1).toIntOrNull()?.takeIf { it in 1..99 }
            ?: return@mapNotNull null
        token.substring(0, separator) to value
    }
    .toMap()

private fun formatMoney(value: Long, minorUnitScale: Int, currencyLabel: String): String {
    if (value < 0) return "价格不清"
    val rendered = if (minorUnitScale == 1) {
        value.toString()
    } else {
        String.format(Locale.ROOT, "%.2f", value.toDouble() / minorUnitScale.toDouble())
            .trimEnd('0')
            .trimEnd('.')
    }
    return "$rendered $currencyLabel".trim()
}

private fun displayItemPrice(
    item: InteractiveCollectionItem,
    minorUnitScale: Int,
    currencyLabel: String,
): String {
    val label = item.priceLabel.trim()
    if (label.isBlank()) return formatMoney(item.unitPriceMinor, minorUnitScale, currencyLabel)
    if (
        item.unitPriceMinor >= 0 &&
        currencyLabel.isNotBlank() &&
        !label.contains(currencyLabel, ignoreCase = true)
    ) {
        return "$label $currencyLabel"
    }
    return label
}
