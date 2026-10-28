package dev.compa.bridge

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update

enum class ConnectionState { STOPPED, CONNECTING, CONNECTED, RECONNECTING, ERROR }

data class BridgeEvent(
    val seq: Long = 0,
    val type: String = "",
    val title: String = "",
    val body: String = "",
    val reply: String = "",
    val conversationId: String = "",
    val approvalId: String = "",
    val at: Double = 0.0,
)

object BridgeState {
    val connection = MutableStateFlow(ConnectionState.STOPPED)
    val detail = MutableStateFlow("Not connected")
    val events = MutableStateFlow<List<BridgeEvent>>(emptyList())

    fun set(state: ConnectionState, text: String) {
        connection.value = state
        detail.value = text
    }

    fun add(event: BridgeEvent) {
        if (event.type !in setOf(
                "final",
                "notification",
                "error",
                "action_required",
                "display_required",
                "cancelled",
            )
        ) {
            return
        }
        events.update { current ->
            val withoutDuplicate = current.filterNot { it.seq == event.seq && event.seq > 0 }
            (withoutDuplicate + event).takeLast(100)
        }
    }
}
