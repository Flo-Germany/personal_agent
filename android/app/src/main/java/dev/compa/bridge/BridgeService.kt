package dev.compa.bridge

import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.content.ContextCompat
import androidx.work.BackoffPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit
import kotlin.math.min

class BridgeService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var prefs: Prefs
    private var socket: WebSocket? = null
    private var attempt = 0
    private var stopping = false
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private val reconnect = Runnable { connect() }

    override fun onCreate() {
        super.onCreate()
        instance = this
        prefs = Prefs(this)
        Notifier.createChannels(this)
        observeNetwork()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopBridge()
            return START_NOT_STICKY
        }
        stopping = false
        startForegroundCompat(Notifier.serviceNotification(this, "Connecting to your desktop…"))
        connect()
        return START_STICKY
    }

    override fun onDestroy() {
        stopping = true
        handler.removeCallbacksAndMessages(null)
        socket?.close(1000, "service stopped")
        socket = null
        networkCallback?.let {
            runCatching {
                getSystemService(ConnectivityManager::class.java)
                    .unregisterNetworkCallback(it)
            }
        }
        instance = null
        BridgeState.set(ConnectionState.STOPPED, "Bridge stopped")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startForegroundCompat(notification: android.app.Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                Notifier.SERVICE_NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(Notifier.SERVICE_NOTIFICATION_ID, notification)
        }
    }

    private fun connect() {
        if (stopping) return
        if (prefs.host.isBlank() || prefs.token.isBlank()) {
            BridgeState.set(ConnectionState.ERROR, "Enter the desktop IP and pairing token")
            updateServiceNotification("Pairing details are missing")
            return
        }
        socket?.cancel()
        BridgeState.set(ConnectionState.CONNECTING, "Connecting to ${prefs.hostWithPort()}…")
        updateServiceNotification("Connecting to ${prefs.hostWithPort()}…")
        val request = Request.Builder()
            .url(prefs.socketUrl())
            .build()
        socket = client.newWebSocket(request, listener)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            attempt = 0
            BridgeState.set(ConnectionState.CONNECTED, "Connected to ${prefs.hostWithPort()}")
            updateServiceNotification("Connected · waiting for tasks")
            webSocket.send(BridgeProtocol.hello(prefs.token))
            BridgeState.add(BridgeEvent(type = "hello"))
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val event = BridgeProtocol.parse(text, prefs.lastSeq) ?: return
            if (event.seq > prefs.lastSeq) prefs.lastSeq = event.seq
            if (event.conversationId.isNotBlank()) prefs.conversationId = event.conversationId
            BridgeState.add(event)
            Notifier.notifyEvent(this@BridgeService, event)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (stopping) return
            BridgeState.set(
                ConnectionState.RECONNECTING,
                "Lost connection (${t.message ?: "network error"}). Retrying…",
            )
            updateServiceNotification("Reconnecting after a network drop…")
            scheduleRetry()
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (stopping) return
            BridgeState.set(ConnectionState.RECONNECTING, "Connection closed. Retrying…")
            scheduleRetry()
        }
    }

    private fun scheduleRetry() {
        handler.removeCallbacks(reconnect)
        val delay = min(30_000L, 1_000L shl min(attempt, 5))
        attempt += 1
        handler.postDelayed(reconnect, delay)
        val poll = OneTimeWorkRequestBuilder<PollWorker>()
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(this).enqueue(poll)
    }

    private fun observeNetwork() {
        val manager = getSystemService(ConnectivityManager::class.java)
        networkCallback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (BridgeState.connection.value != ConnectionState.CONNECTED) {
                    handler.removeCallbacks(reconnect)
                    handler.postDelayed(reconnect, 500)
                }
            }
        }
        runCatching { manager.registerDefaultNetworkCallback(networkCallback!!) }
    }

    private fun updateServiceNotification(text: String) {
        getSystemService(android.app.NotificationManager::class.java)
            .notify(
                Notifier.SERVICE_NOTIFICATION_ID,
                Notifier.serviceNotification(this, text),
            )
    }

    private fun stopBridge() {
        stopping = true
        prefs.serviceEnabled = false
        handler.removeCallbacksAndMessages(null)
        socket?.close(1000, "user stopped")
        socket = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    companion object {
        const val ACTION_STOP = "dev.compa.bridge.STOP"
        @Volatile private var instance: BridgeService? = null

        fun start(context: Context) {
            val intent = Intent(context, BridgeService::class.java)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, BridgeService::class.java).apply {
                action = ACTION_STOP
            }
            context.startService(intent)
        }

        fun isConnected(): Boolean =
            instance != null && BridgeState.connection.value == ConnectionState.CONNECTED

        fun sendTask(text: String): Boolean {
            val service = instance ?: return false
            val socket = service.socket ?: return false
            val payload = BridgeProtocol.task(text, service.prefs.conversationId)
            return socket.send(payload)
        }
    }
}
