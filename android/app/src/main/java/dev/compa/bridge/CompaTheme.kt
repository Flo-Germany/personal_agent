package dev.compa.bridge

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val Blue = Color(0xFF0068E0)
private val Cyan = Color(0xFF5AC8FA)

private val DarkColors = darkColorScheme(
    primary = Blue,
    onPrimary = Color.White,
    secondary = Cyan,
    background = Color(0xFF07111F),
    onBackground = Color(0xFFF2F6FC),
    surface = Color(0xFF0D1A2B),
    onSurface = Color(0xFFF2F6FC),
    surfaceVariant = Color(0xFF15263B),
    onSurfaceVariant = Color(0xFFB8C6D9),
)

private val LightColors = lightColorScheme(
    primary = Blue,
    onPrimary = Color.White,
    secondary = Color(0xFF00629E),
    background = Color(0xFFF2F5FA),
    onBackground = Color(0xFF111820),
    surface = Color.White,
    onSurface = Color(0xFF111820),
    surfaceVariant = Color(0xFFE6EBF3),
    onSurfaceVariant = Color(0xFF465468),
)

@Composable
fun CompaTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}
