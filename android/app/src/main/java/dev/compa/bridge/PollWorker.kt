package dev.compa.bridge

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class PollWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (BridgeService.isConnected()) return@withContext Result.success()
        val prefs = Prefs(applicationContext)
        if (prefs.host.isBlank() || prefs.token.isBlank()) {
            return@withContext Result.success()
        }
        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build()
        try {
            client.newCall(Request.Builder().url(prefs.pollUrl(prefs.lastSeq)).build())
                .execute()
                .use { response ->
                    if (!response.isSuccessful) {
                        return@withContext if (response.code == 401) {
                            Result.failure()
                        } else {
                            Result.retry()
                        }
                    }
                    val body = response.body?.string().orEmpty()
                    val json = JSONObject(body)
                    val events = json.optJSONArray("events") ?: return@withContext Result.success()
                    for (index in 0 until events.length()) {
                        val event = BridgeProtocol.parse(
                            events.getJSONObject(index).toString(),
                            prefs.lastSeq,
                        ) ?: continue
                        if (event.seq > prefs.lastSeq) prefs.lastSeq = event.seq
                        if (event.conversationId.isNotBlank()) {
                            prefs.conversationId = event.conversationId
                        }
                        BridgeState.add(event)
                        Notifier.notifyEvent(applicationContext, event)
                    }
                    val cursor = json.optLong("cursor", prefs.lastSeq)
                    if (cursor > prefs.lastSeq) prefs.lastSeq = cursor
                    Result.success()
                }
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        const val UNIQUE_NAME = "compa_poll"
    }
}
