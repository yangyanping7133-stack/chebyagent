package com.cheby.codex.mobile.ui.rich

import java.io.StringReader
import java.net.URI
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Node
import org.xml.sax.InputSource
import org.xml.sax.helpers.DefaultHandler

/** A bounded, non-executable HTML fragment; never pass raw model HTML to WebView. */
internal data class PosterDocument(
    val html: String,
    val imageUrls: Set<String>,
    val links: Set<String>,
    val text: String,
)

internal fun publicHttpsUrl(value: String): String? = runCatching {
    require(value.length in 1..2048 && value.none { it.isWhitespace() || it.code < 32 })
    val uri = URI(value)
    val host = uri.host?.lowercase() ?: error("host")
    require(uri.scheme == "https" && uri.rawUserInfo == null && uri.port in listOf(-1, 443))
    require(host.contains('.') && !host.endsWith('.') && ':' !in host)
    require(!host.matches(Regex("[0-9.]+")))
    require(listOf("localhost", "local", "internal", "test", "invalid").none { host == it || host.endsWith(".$it") })
    value
}.getOrNull()

internal fun parsePoster(source: String): PosterDocument? = runCatching {
    require(source.length in 1..65_536)
    // No DTD, custom entities, processing instructions or comments. XML's predefined escapes only.
    require(!source.contains("<!") && !source.contains("<?"))
    val factory = DocumentBuilderFactory.newInstance().apply {
        isNamespaceAware = false
        isExpandEntityReferences = false
    }
    val builder = factory.newDocumentBuilder().apply { setErrorHandler(DefaultHandler()) }
    val root = builder.parse(InputSource(StringReader("<poster>$source</poster>"))).documentElement
    val images = linkedSetOf<String>()
    val links = linkedSetOf<String>()
    var count = 0
    val text = StringBuilder()
    fun render(node: Node, depth: Int): String {
        require(++count <= 512 && depth <= 16)
        if (node.nodeType == Node.TEXT_NODE) {
            text.append(node.nodeValue).append(' ')
            return escapePoster(node.nodeValue)
        }
        require(node.nodeType == Node.ELEMENT_NODE)
        val tag = node.nodeName.lowercase()
        require(tag in POSTER_TAGS || tag == "poster")
        val attrs = StringBuilder()
        for (index in 0 until node.attributes.length) {
            val attr = node.attributes.item(index)
            when (attr.nodeName.lowercase()) {
                "href" -> {
                    require(tag == "a")
                    val url = requireNotNull(publicHttpsUrl(attr.nodeValue))
                    links.add(url)
                    attrs.append(" href=\"").append(escapePoster(url)).append('"')
                }
                "src" -> {
                    require(tag == "img")
                    val url = requireNotNull(posterImageUrl(attr.nodeValue))
                    images.add(url)
                    require(images.size <= 12)
                    attrs.append(" src=\"").append(escapePoster(url)).append('"')
                }
                "alt", "title" -> {
                    require(attr.nodeValue.length <= 300)
                    attrs.append(" ").append(attr.nodeName.lowercase()).append("=\"")
                        .append(escapePoster(attr.nodeValue)).append('"')
                    if (tag == "img") text.append(attr.nodeValue).append(' ')
                }
                "class" -> {
                    require(attr.nodeValue.split(' ').all { it in POSTER_CLASSES })
                    attrs.append(" class=\"").append(escapePoster(attr.nodeValue)).append('"')
                }
                // Unknown attributes, including event handlers, are discarded.
            }
        }
        val children = (0 until node.childNodes.length).joinToString("") { render(node.childNodes.item(it), depth + 1) }
        if (tag == "poster") return children
        if (tag == "img" || tag == "br" || tag == "hr") return "<$tag$attrs/>"
        return "<$tag$attrs>$children</$tag>"
    }
    val body = render(root, 0)
    require(text.isNotBlank())
    PosterDocument("""<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"/>
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"/>
        <style>$POSTER_CSS</style></head><body>$body</body></html>""".trimIndent(), images, links, text.toString().trim())
}.getOrNull()

private fun escapePoster(value: String): String = value.replace("&", "&amp;")
    .replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&#39;")

internal fun posterImageUrl(value: String): String? = if (
    Regex("cheby-image:phone-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}\\.(jpg|png|webp)").matches(value)
) "https://poster.invalid/artifacts/" + value.substringAfter(':') else publicHttpsUrl(value)

private val POSTER_TAGS = setOf("article", "section", "header", "footer", "div", "span", "h1", "h2", "h3", "p", "small", "strong", "em", "ul", "ol", "li", "a", "img", "br", "hr")
private val POSTER_CLASSES = setOf("hero", "card", "recommended", "eyebrow", "muted", "facts", "badge", "button", "grid", "caption")
private const val POSTER_CSS = """
*{box-sizing:border-box}body{margin:0;padding:12px;background:#f6f2e9;color:#253b35;font-family:system-ui,sans-serif;font-size:14px;line-height:1.45;overflow-wrap:anywhere}
h1{font-size:23px;line-height:1.2;margin:6px 0 10px;letter-spacing:-.4px}h2{font-size:19px;margin:8px 0}h3{font-size:17px}p{margin:8px 0}a{color:#216451}
.hero{padding:0 0 10px}.eyebrow{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:#6e7866}.muted,.caption,small{color:#626c61;font-size:12px}
.card{padding:12px;margin:0 0 12px;border-radius:16px;background:#fffdf8;border:1px solid #dedfd1}.recommended{border:2px solid #315c4b}
img{display:block;width:100%;max-height:180px;object-fit:cover;border-radius:10px;background:#e5e6da;min-height:90px;color:#626c61}
.badge{display:inline-block;border-radius:20px;padding:4px 10px;background:#e7efdf;color:#315c4b;font-size:12px;font-weight:600}
.facts{display:flex;flex-wrap:wrap;gap:8px;font-size:13px}.button{display:inline-block;padding:10px 15px;border-radius:12px;background:#315c4b;color:white;text-decoration:none;margin:8px 6px 0 0;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}hr{border:0;border-top:1px solid #d9ddce;margin:18px 0}li{margin:6px 0}
"""
