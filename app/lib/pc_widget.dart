import 'package:home_widget/home_widget.dart';

/// Pushes the saved PC's name and the live connection state to the Android
/// home screen widget.
///
/// The data travels through home_widget's shared preferences; the native
/// PcStatusWidgetProvider reads `pcName`/`connected` from them whenever
/// [sync] triggers an update. All failures are swallowed — the widget is a
/// convenience, never a reason to break the app.
class PcWidget {
  PcWidget._();

  /// Class name of the Android AppWidgetProvider (AndroidManifest receiver).
  static const _androidName = 'PcStatusWidgetProvider';

  /// Stores [pcName]/[connected] and asks the launcher to redraw the widget.
  static Future<void> sync({
    required String pcName,
    required bool connected,
  }) async {
    try {
      await HomeWidget.saveWidgetData<String>('pcName', pcName);
      await HomeWidget.saveWidgetData<bool>('connected', connected);
      await HomeWidget.updateWidget(androidName: _androidName);
    } catch (_) {
      // Best effort (e.g. unsupported platform, no launcher widget added).
    }
  }
}
