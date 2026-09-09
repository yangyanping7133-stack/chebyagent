package com.cheby.codex.mobile.ui.rich

import android.content.Intent
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.ClipData
import android.content.ClipboardManager
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Canvas
import android.net.Uri
import android.os.Build
import android.view.MotionEvent
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.key
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.viewinterop.AndroidView
import java.io.ByteArrayInputStream
import java.net.InetAddress
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import okhttp3.Dns
import okhttp3.OkHttpClient
import okhttp3.Request
import kotlin.math.ceil

@Composable
internal fun PosterView(document: PosterDocument) {
    key(document.html) { PosterViewContent(document) }
}

@Composable
private fun PosterViewContent(document: PosterDocument) {
    val context = LocalContext.current
    val density = LocalDensity.current
    // LazyColumn disposes offscreen rows. Preserve measured height when scrolling back,
    // instead of collapsing an old document to one pixel and moving the scroll anchor.
    val heightPx = rememberSaveable(document.html, density.density) {
        // Only a never-measured document starts small. A tall initial viewport can
        // become contentHeight's floor, leaving blank space under a short poster.
        mutableStateOf(1)
    }
    val view = remember(document) { mutableStateOf<WebView?>(null) }
    DisposableEffect(document) { onDispose { view.value?.destroy(); view.value = null } }
    Column {
        AndroidView(
            modifier = Modifier.fillMaxWidth().height(with(density) { heightPx.value.toDp() }),
            factory = { ctx -> InlinePosterWebView(ctx) { heightPx.value = it }.apply {
                view.value = this
                setBackgroundColor(Color.TRANSPARENT)
                contentDescription = "图文推荐海报。随聊天上下滚动，可点击来源链接。"
                settings.javaScriptEnabled = false
                settings.allowFileAccess = false
                settings.allowContentAccess = false
                settings.domStorageEnabled = false
                settings.setSupportMultipleWindows(false)
                settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
                setOnLongClickListener {
                    val url = hitTestResult.extra
                    if (url != null && url in document.links) {
                        (context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                            .setPrimaryClip(ClipData.newPlainText("推荐链接", url))
                        Toast.makeText(context, "链接已复制", Toast.LENGTH_SHORT).show()
                        true
                    } else false
                }
                webViewClient = object : WebViewClient() {
                    override fun shouldInterceptRequest(v: WebView, request: WebResourceRequest): WebResourceResponse {
                        val url = request.url.toString()
                        return if (!request.isForMainFrame && request.method == "GET" && url in document.imageUrls) {
                            if (url.startsWith("https://poster.invalid/artifacts/")) localPosterImage(ctx.filesDir, url) else posterImage(url)
                        } else blockedPosterResource()
                    }
                    override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                        val url = request.url.toString()
                        if (request.isForMainFrame && request.hasGesture() && url in document.links) {
                            runCatching {
                                openNativePosterLink(context, url)
                            }.onFailure { Toast.makeText(context, "未找到对应应用，未打开浏览器。可长按链接复制。", Toast.LENGTH_LONG).show() }
                        }
                        return true
                    }
                }
                loadDataWithBaseURL("https://poster.invalid/", document.html, "text/html", "UTF-8", null)
            } },
        )
        Text("点击打开应用 · 长按链接复制", style = MaterialTheme.typography.labelSmall)
    }
}

/** A document in the timeline, not a second scroll container. No JS measurement bridge. */
private class InlinePosterWebView(context: Context, private val onHeight: (Int) -> Unit) : WebView(context) {
    private var reportedHeight = 0
    private var disposed = false
    private val reportHeight = Runnable { if (!disposed) onHeight(reportedHeight) }

    init {
        isVerticalScrollBarEnabled = false
        isHorizontalScrollBarEnabled = false
        overScrollMode = OVER_SCROLL_NEVER
    }

    @Suppress("DEPRECATION") // WebView contentHeight is CSS px; scale converts to physical px.
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val measured = posterHeightPx(contentHeight, scale) ?: return
        if (measured != reportedHeight) {
            reportedHeight = measured
            removeCallbacks(reportHeight)
            // Images can change the layout after pageFinished. Report only changed heights,
            // outside draw, so Compose can resize without a continuous polling/layout loop.
            post(reportHeight)
        }
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val handled = super.onTouchEvent(event)
        // Retain clicks/long presses, but let LazyColumn intercept vertical drags immediately.
        parent?.requestDisallowInterceptTouchEvent(false)
        return handled
    }

    override fun canScrollVertically(direction: Int) = false

    override fun onScrollChanged(l: Int, t: Int, oldl: Int, oldt: Int) {
        super.onScrollChanged(l, t, oldl, oldt)
        if (l != 0 || t != 0) scrollTo(0, 0)
    }

    override fun destroy() {
        disposed = true
        removeCallbacks(reportHeight)
        super.destroy()
    }
}

internal fun posterHeightPx(cssHeight: Int, scale: Float): Int? {
    if (cssHeight <= 0 || !scale.isFinite() || scale <= 0) return null
    return ceil(cssHeight.toDouble() * scale).toInt().coerceAtLeast(1)
}

/** Never substitute a browser for the app the user expects a result link to open. */
private fun openNativePosterLink(context: Context, url: String) {
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_REQUIRE_NON_BROWSER))
        return
    }
    // Older Android has no platform non-browser flag and no package-visibility restriction.
    val manager = context.packageManager
    val generic = Intent(Intent.ACTION_VIEW, Uri.parse("https://example.com/")).addCategory(Intent.CATEGORY_BROWSABLE)
    val browsers = manager.queryIntentActivities(generic, PackageManager.MATCH_DEFAULT_ONLY)
        .map { it.activityInfo.packageName }.toSet()
    val targets = manager.queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY)
        .map { it.activityInfo.packageName }.distinct().filterNot { it in browsers }
    if (targets.size != 1) throw ActivityNotFoundException("No unambiguous native handler")
    context.startActivity(intent.setPackage(targets.single()))
}

private fun blockedPosterResource() = WebResourceResponse("text/plain", "UTF-8", ByteArrayInputStream(ByteArray(0)))

private fun localPosterImage(files: java.io.File, url: String): WebResourceResponse = runCatching {
    val name = url.substringAfterLast('/')
    require(posterImageUrl("cheby-image:$name") == url)
    val root = java.io.File(files, "usr/var/lib/cheby-runtime/debian-rootfs/root/.cheby/phonebridge/artifacts").canonicalFile
    val file = java.io.File(root, name)
    require(file.canonicalFile.parentFile == root && file.isFile && file.length() in 1..8 * 1024 * 1024)
    val mime = when (file.extension) { "png" -> "image/png"; "webp" -> "image/webp"; else -> "image/jpeg" }
    WebResourceResponse(mime, null, ByteArrayInputStream(file.readBytes()))
}.getOrElse { blockedPosterResource() }

private val posterHttp = OkHttpClient.Builder()
    .followRedirects(false).followSslRedirects(false)
    .callTimeout(10, TimeUnit.SECONDS)
    .dns(object : Dns {
        override fun lookup(hostname: String): List<InetAddress> = Dns.SYSTEM.lookup(hostname).also { addresses ->
            if (addresses.isEmpty() || addresses.any { !isPublicPosterAddress(it) }) throw UnknownHostException("Non-public image address")
        }
    }).build()

internal fun isPublicPosterAddress(address: InetAddress): Boolean {
    if (address.isAnyLocalAddress || address.isLoopbackAddress || address.isLinkLocalAddress ||
        address.isSiteLocalAddress || address.isMulticastAddress) return false
    val bytes = address.address.map { it.toInt() and 255 }
    if (bytes.size == 16) return (bytes[0] and 0xe0) == 0x20 // global unicast only
    return bytes.size == 4 && bytes[0] !in setOf(0, 10, 127, 169, 192, 198, 203) &&
        bytes[0] < 224 && !(bytes[0] == 100 && bytes[1] in 64..127)
}

private fun posterImage(url: String): WebResourceResponse = runCatching {
    require(publicHttpsUrl(url) != null)
    posterHttp.newCall(Request.Builder().url(url).header("Accept", "image/jpeg,image/png,image/webp").build()).execute().use { response ->
        require(response.isSuccessful)
        val body = requireNotNull(response.body)
        val mime = body.contentType()?.let { "${it.type}/${it.subtype}" }
        require(mime in setOf("image/jpeg", "image/png", "image/webp"))
        require(body.contentLength() <= 4 * 1024 * 1024)
        val bytes = body.byteStream().use { input ->
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                require(output.size() + read <= 4 * 1024 * 1024)
                output.write(buffer, 0, read)
            }
            output.toByteArray()
        }
        WebResourceResponse(mime, null, ByteArrayInputStream(bytes))
    }
}.getOrElse { blockedPosterResource() }
