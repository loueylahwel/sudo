package io.github.loueylahwel.sudo

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build
import android.widget.RemoteViews
import es.antonborri.home_widget.HomeWidgetProvider

/// Home screen widget showing the saved PC's name, a green/gray connection
/// dot and a Lock button. Data (`pcName`, `connected`) is written from Dart
/// via home_widget's shared preferences; updates are event-driven from the
/// app (connect/disconnect), there is no periodic refresh.
class PcStatusWidgetProvider : HomeWidgetProvider() {
    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
        widgetData: SharedPreferences,
    ) {
        val name = widgetData.getString("pcName", "") ?: ""
        val connected = widgetData.getBoolean("connected", false)
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= 23) flags = flags or PendingIntent.FLAG_IMMUTABLE
        for (id in appWidgetIds) {
            val views = RemoteViews(context.packageName, R.layout.pc_status_widget)
            views.setTextViewText(
                R.id.widget_pc_name,
                name.ifEmpty { context.getString(R.string.widget_no_pc) },
            )
            views.setImageViewResource(
                R.id.widget_status_dot,
                if (connected) R.drawable.widget_dot_green else R.drawable.widget_dot_gray,
            )
            // Lock button → WidgetActionReceiver → Dart (or app launch).
            val lockIntent = Intent(context, WidgetActionReceiver::class.java)
                .setAction(WidgetActionReceiver.ACTION_LOCK)
            views.setOnClickPendingIntent(
                R.id.widget_lock_button,
                PendingIntent.getBroadcast(context, 0, lockIntent, flags),
            )
            // Tapping the name just opens the app.
            val launchIntent = Intent(context, MainActivity::class.java)
            views.setOnClickPendingIntent(
                R.id.widget_pc_name,
                PendingIntent.getActivity(context, 1, launchIntent, flags),
            )
            appWidgetManager.updateAppWidget(id, views)
        }
    }
}
