package dev.compa.bridge

import org.json.JSONObject

object BridgeProtocol {
    fun parse(json: String, fallbackSeq: Long = 0): BridgeEvent? {
        return try {
            val obj = JSONObject(json)
            BridgeEvent(
                seq = obj.optLong("seq", fallbackSeq),
                type = obj.optString("type", ""),
                title = obj.optString("title", ""),
                body = obj.optString("body", obj.optString("message", "")),
                reply = obj.optString("reply", ""),
                conversationId = obj.optString("conversation_id", ""),
                approvalId = obj.optString("approval_id", ""),
                at = obj.optDouble("at", 0.0),
            )
        } catch (_: Exception) {
            null
        }
    }

    fun task(text: String, conversationId: String): String {
        val obj = JSONObject()
        obj.put("type", "task")
        obj.put("text", text)
        if (conversationId.isNotBlank()) obj.put("conversation_id", conversationId)
        return obj.toString()
    }

    fun hello(token: String): String {
        val obj = JSONObject()
        obj.put("type", "hello")
        obj.put("token", token)
        return obj.toString()
    }
}
