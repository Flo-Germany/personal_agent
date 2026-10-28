package dev.compa.bridge

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BridgeProtocolTest {
    @Test
    fun parsesFinalReply() {
        val event = BridgeProtocol.parse(
            """{"type":"final","reply":"done","conversation_id":"c1","seq":7}"""
        )
        requireNotNull(event)
        assertEquals("final", event.type)
        assertEquals("done", event.reply)
        assertEquals("c1", event.conversationId)
        assertEquals(7, event.seq)
    }

    @Test
    fun buildsTaskWithConversation() {
        val json = BridgeProtocol.task("hello", "c1")
        assertTrue(json.contains("\"type\":\"task\""))
        assertTrue(json.contains("\"conversation_id\":\"c1\""))
    }

    @Test
    fun malformedJsonIsIgnored() {
        assertEquals(null, BridgeProtocol.parse("not-json"))
    }
}
