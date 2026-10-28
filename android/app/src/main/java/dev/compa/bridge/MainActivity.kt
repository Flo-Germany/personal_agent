package dev.compa.bridge

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.speech.RecognizerIntent
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.BatteryAlert
import androidx.compose.material.icons.rounded.Mic
import androidx.compose.material.icons.rounded.PowerSettingsNew
import androidx.compose.material.icons.automirrored.rounded.Send
import androidx.compose.material.icons.rounded.Wifi
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Notifier.createChannels(this)
        setContent {
            CompaTheme { CompaScreen() }
        }
    }
}

private fun ignoringBattery(context: android.content.Context): Boolean {
    val manager = context.getSystemService(PowerManager::class.java)
    return manager.isIgnoringBatteryOptimizations(context.packageName)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CompaScreen() {
    val context = LocalContext.current
    val prefs = remember { Prefs(context) }
    var host by rememberSaveable { mutableStateOf(prefs.host) }
    var token by rememberSaveable { mutableStateOf(prefs.token) }
    var message by rememberSaveable { mutableStateOf("") }
    var showBatteryDialog by rememberSaveable { mutableStateOf(!prefs.batteryAsked) }
    var batteryOk by remember { mutableStateOf(ignoringBattery(context)) }
    val connection by BridgeState.connection.collectAsState()
    val detail by BridgeState.detail.collectAsState()
    val events by BridgeState.events.collectAsState()
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()

    val notificationPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }

    val speechLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val spoken = result.data
                ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                ?.firstOrNull()
            if (!spoken.isNullOrBlank()) message = spoken
        }
    }

    fun startBridge() {
        prefs.host = host
        prefs.token = token
        prefs.serviceEnabled = true
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        BridgeService.start(context)
    }

    LaunchedEffect(Unit) {
        if (prefs.serviceEnabled && BridgeState.connection.value == ConnectionState.STOPPED) {
            BridgeService.start(context)
        }
    }

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                batteryOk = ignoringBattery(context)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbar) },
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("Compa", fontWeight = FontWeight.SemiBold)
                        Text(
                            "Android companion",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                actions = {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.padding(end = 12.dp),
                    ) {
                        Box(
                            Modifier
                                .size(8.dp)
                                .background(
                                    when (connection) {
                                        ConnectionState.CONNECTED -> Color(0xFF30D158)
                                        ConnectionState.CONNECTING,
                                        ConnectionState.RECONNECTING -> Color(0xFFFF9F0A)
                                        else -> Color(0xFF8A94A6)
                                    },
                                    CircleShape,
                                )
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            when (connection) {
                                ConnectionState.CONNECTED -> "Connected"
                                ConnectionState.CONNECTING -> "Connecting"
                                ConnectionState.RECONNECTING -> "Retrying"
                                ConnectionState.ERROR -> "Check setup"
                                ConnectionState.STOPPED -> "Offline"
                            },
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 14.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                ),
                shape = RoundedCornerShape(20.dp),
            ) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("Desktop pairing", fontWeight = FontWeight.SemiBold)
                    OutlinedTextField(
                        value = host,
                        onValueChange = { host = it },
                        label = { Text("Desktop IP or host:port") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    OutlinedTextField(
                        value = token,
                        onValueChange = { token = it },
                        label = { Text("Pairing token") },
                        singleLine = true,
                        visualTransformation = PasswordVisualTransformation(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Text(
                        detail,
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(
                            onClick = { startBridge() },
                            shape = RoundedCornerShape(14.dp),
                        ) {
                            Icon(Icons.Rounded.Wifi, null, Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("Connect")
                        }
                        OutlinedButton(
                            onClick = {
                                BridgeService.stop(context)
                                BridgeState.set(ConnectionState.STOPPED, "Bridge stopped")
                            },
                            shape = RoundedCornerShape(14.dp),
                        ) {
                            Icon(Icons.Rounded.PowerSettingsNew, null, Modifier.size(16.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("Stop")
                        }
                    }
                    Text(
                        "Keep this service running in the background. Android will show a low-priority notification.",
                        fontSize = 10.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            if (!batteryOk) {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = Color(0x22FF9F0A),
                    ),
                    shape = RoundedCornerShape(20.dp),
                ) {
                    Row(
                        Modifier.padding(14.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            Icons.Rounded.BatteryAlert,
                            null,
                            tint = Color(0xFFFF9F0A),
                            modifier = Modifier.size(20.dp),
                        )
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text("Keep the bridge alive", fontWeight = FontWeight.SemiBold)
                            Text(
                                "ColorOS and similar ROMs kill background sockets. Allow unrestricted battery use.",
                                fontSize = 10.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        TextButton(onClick = { requestBatteryExemption(context) }) {
                            Text("Allow")
                        }
                    }
                }
            }

            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("Activity", fontWeight = FontWeight.SemiBold)
                Text(
                    "${events.size} events",
                    fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (events.isEmpty()) {
                    item {
                        Text(
                            "Send a task from your phone or wait for a desktop update.",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(vertical = 10.dp),
                        )
                    }
                }
                items(events.reversed()) { event ->
                    EventCard(event)
                }
            }

            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(bottom = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                OutlinedTextField(
                    value = message,
                    onValueChange = { message = it },
                    placeholder = { Text("Ask Compa on your desktop…") },
                    modifier = Modifier.weight(1f),
                    maxLines = 3,
                    shape = RoundedCornerShape(18.dp),
                )
                OutlinedButton(
                    onClick = {
                        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                            putExtra(
                                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM,
                            )
                            putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak your task")
                        }
                        runCatching { speechLauncher.launch(intent) }
                            .onFailure {
                                scope.launch {
                                    snackbar.showSnackbar("No speech recognizer installed")
                                }
                            }
                    },
                    shape = CircleShape,
                    modifier = Modifier.size(48.dp),
                    contentPadding = PaddingValues(0.dp),
                ) {
                    Icon(
                        Icons.Rounded.Mic,
                        contentDescription = "Voice input",
                        modifier = Modifier.size(22.dp),
                    )
                }
                Button(
                    onClick = {
                        val clean = message.trim()
                        if (clean.isEmpty()) return@Button
                        if (BridgeService.sendTask(clean)) {
                            message = ""
                        } else {
                            scope.launch {
                                snackbar.showSnackbar("Not connected. Tap Connect first.")
                            }
                        }
                    },
                    shape = CircleShape,
                    modifier = Modifier.size(48.dp),
                    contentPadding = PaddingValues(0.dp),
                ) {
                    Icon(
                        Icons.AutoMirrored.Rounded.Send,
                        contentDescription = "Send task",
                        modifier = Modifier.size(22.dp),
                    )
                }
            }
        }
    }

    if (showBatteryDialog) {
        AlertDialog(
            onDismissRequest = {
                prefs.batteryAsked = true
                showBatteryDialog = false
            },
            icon = { Icon(Icons.Rounded.BatteryAlert, null) },
            title = { Text("Keep Compa reachable") },
            text = {
                Text(
                    "Android may stop the WebSocket while the screen is off. " +
                        "Allow Compa to ignore battery optimizations so task " +
                        "results arrive as notifications. WorkManager polling " +
                        "is the fallback if the ROM still stops the service."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    prefs.batteryAsked = true
                    showBatteryDialog = false
                    requestBatteryExemption(context)
                    batteryOk = ignoringBattery(context)
                }) { Text("Allow") }
            },
            dismissButton = {
                TextButton(onClick = {
                    prefs.batteryAsked = true
                    showBatteryDialog = false
                }) { Text("Later") }
            },
        )
    }
}

@Composable
private fun EventCard(event: BridgeEvent) {
    val (title, body) = when (event.type) {
        "final" -> "Compa finished" to event.reply
        "notification" -> event.title.ifBlank { "Compa" } to event.body
        "action_required" -> "Approval needed" to event.body
        "error" -> "Error" to event.body
        else -> event.type to event.body
    }
    Card(
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant,
        ),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
            Text(
                title,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface,
            )
            if (body.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                Text(
                    body.take(700),
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

private fun requestBatteryExemption(context: android.content.Context) {
    val uri = Uri.parse("package:${context.packageName}")
    val direct = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, uri)
    runCatching { context.startActivity(direct) }
        .onFailure {
            runCatching {
                context.startActivity(
                    Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                )
            }
        }
}
