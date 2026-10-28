package dev.compa.bridge

import android.content.Context
import java.net.URLEncoder

class Prefs(context: Context) {
    private val store = context.getSharedPreferences("compa_bridge", Context.MODE_PRIVATE)

    var host: String
        get() = store.getString("host", "172.16.1.210").orEmpty()
        set(value) = store.edit().putString("host", value.trim()).apply()

    var token: String
        get() = store.getString("token", "").orEmpty()
        set(value) = store.edit().putString("token", value.trim()).apply()

    var conversationId: String
        get() = store.getString("conversation_id", "").orEmpty()
        set(value) = store.edit().putString("conversation_id", value).apply()

    var lastSeq: Long
        get() = store.getLong("last_seq", 0L)
        set(value) = store.edit().putLong("last_seq", value).apply()

    var batteryAsked: Boolean
        get() = store.getBoolean("battery_asked", false)
        set(value) = store.edit().putBoolean("battery_asked", value).apply()

    /** Resume the foreground service when the app is reopened. */
    var serviceEnabled: Boolean
        get() = store.getBoolean("service_enabled", false)
        set(value) = store.edit().putBoolean("service_enabled", value).apply()

    fun hostWithPort(): String {
        val clean = host
            .trim()
            .removePrefix("ws://")
            .removePrefix("wss://")
            .removeSuffix("/")
        return if (clean.contains(":")) clean else "$clean:8765"
    }

    fun socketUrl(): String {
        val encoded = URLEncoder.encode(token, "UTF-8")
        return "ws://${hostWithPort()}/ws?token=$encoded"
    }

    fun pollUrl(since: Long): String {
        val encoded = URLEncoder.encode(token, "UTF-8")
        return "http://${hostWithPort()}/api/poll?token=$encoded&since=$since"
    }
}
