package dev.compa.bridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat

object Notifier {
    const val CONNECTION_CHANNEL = "compa_connection"
    const val EVENT_CHANNEL = "compa_events"
    const val APPROVAL_CHANNEL = "compa_approvals"
    const val SERVICE_NOTIFICATION_ID = 4201

    fun createChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                CONNECTION_CHANNEL,
                "Desktop connection",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Keeps the Compa WebSocket alive"
                setShowBadge(false)
            }
        )
        manager.createNotificationChannel(
            NotificationChannel(
                EVENT_CHANNEL,
                "Compa updates",
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply { description = "Task results and desktop notifications" }
        )
        manager.createNotificationChannel(
            NotificationChannel(
                APPROVAL_CHANNEL,
                "Approvals",
                NotificationManager.IMPORTANCE_HIGH,
            ).apply { description = "Actions waiting for your approval" }
        )
    }

    private fun openIntent(context: Context): PendingIntent =
        PendingIntent.getActivity(
            context,
            0,
            Intent(context, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    fun serviceNotification(context: Context, text: String): Notification =
        NotificationCompat.Builder(context, CONNECTION_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_compa)
            .setContentTitle("Compa")
            .setContentText(text)
            .setContentIntent(openIntent(context))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

    fun notifyEvent(context: Context, event: BridgeEvent) {
        val title: String
        val body: String
        val channel: String
        when (event.type) {
            "final" -> {
                title = "Compa finished"
                body = event.reply.ifBlank { "Your task is done." }.take(900)
                channel = EVENT_CHANNEL
            }
            "notification" -> {
                title = event.title.ifBlank { "Compa" }
                body = event.body.ifBlank { "Status update from your desktop." }
                channel = EVENT_CHANNEL
            }
            "action_required" -> {
                title = "Approval needed"
                body = event.body.ifBlank { "Open Compa to review the action." }
                channel = APPROVAL_CHANNEL
            }
            "display_required" -> {
                title = "Choose a display"
                body = "Compa needs to know which screen to capture."
                channel = APPROVAL_CHANNEL
            }
            "error" -> {
                title = "Compa hit an error"
                body = event.body.ifBlank { "Open Compa for details." }
                channel = EVENT_CHANNEL
            }
            else -> return
        }
        val notification = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_compa)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(openIntent(context))
            .setAutoCancel(true)
            .setPriority(
                if (channel == APPROVAL_CHANNEL) NotificationCompat.PRIORITY_HIGH
                else NotificationCompat.PRIORITY_DEFAULT
            )
            .build()
        val id = if (event.seq > 0) (event.seq % Int.MAX_VALUE).toInt() else System.currentTimeMillis().toInt()
        context.getSystemService(NotificationManager::class.java).notify(id, notification)
    }
}
