package com.cheby.codex.mobile.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

val Ink = Color(0xFF1F2329)
val Canvas = Color(0xFFF5F6F8)
val Paper = Color(0xFFFFFFFF)
val SignalBlue = Color(0xFF3370FF)
val CompleteGreen = Color(0xFF00A870)
val WarningAmber = Color(0xFFD97706)
val RiskRed = Color(0xFFF54A45)
val Muted = Color(0xFF646A73)
val Divider = Color(0x1F1F2329)
val BlueSoft = Color(0xFFEDF3FF)
val GreenSoft = Color(0xFFEAF8F2)
val AmberSoft = Color(0xFFFFF4E5)
val RedSoft = Color(0xFFFFECEB)

private val ChebyColors = lightColorScheme(
    primary = SignalBlue,
    onPrimary = Paper,
    primaryContainer = BlueSoft,
    onPrimaryContainer = Ink,
    secondary = CompleteGreen,
    onSecondary = Paper,
    error = RiskRed,
    onError = Paper,
    background = Canvas,
    onBackground = Ink,
    surface = Paper,
    onSurface = Ink,
    surfaceVariant = Color(0xFFF0F1F3),
    onSurfaceVariant = Muted,
    outline = Divider,
)

private val ChebyTypography = Typography(
    headlineSmall = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 22.sp,
        lineHeight = 28.sp,
        fontWeight = FontWeight.Bold,
    ),
    titleLarge = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 20.sp,
        lineHeight = 26.sp,
        fontWeight = FontWeight.Bold,
    ),
    titleMedium = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 16.sp,
        lineHeight = 22.sp,
        fontWeight = FontWeight.SemiBold,
    ),
    bodyLarge = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 16.sp,
        lineHeight = 24.sp,
    ),
    bodyMedium = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 14.sp,
        lineHeight = 21.sp,
    ),
    labelLarge = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 14.sp,
        lineHeight = 20.sp,
        fontWeight = FontWeight.SemiBold,
    ),
    labelMedium = androidx.compose.ui.text.TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontSize = 12.sp,
        lineHeight = 18.sp,
        fontWeight = FontWeight.Medium,
    ),
)

@Composable
fun ChebyCodexTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = ChebyColors,
        typography = ChebyTypography,
        content = content,
    )
}
