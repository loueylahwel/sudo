import 'package:flutter/services.dart';

const MethodChannel _channel = MethodChannel('pcocket/orientation');

/// Landscape, following the phone either way round — even with the
/// system's auto-rotate off (MainActivity: SENSOR_LANDSCAPE). Flutter's own
/// landscapeLeft+landscapeRight obeys the rotation lock, so turning the
/// phone over left the PC screen upside down.
Future<void> lockSensorLandscape() async {
  try {
    await _channel.invokeMethod<void>('sensorLandscape');
  } on MissingPluginException {
    await SystemChrome.setPreferredOrientations(const [
      DeviceOrientation.landscapeLeft,
      DeviceOrientation.landscapeRight,
    ]);
  }
}

/// Hands orientation back to the system (and the user's rotation lock).
Future<void> unlockOrientation() async {
  try {
    await _channel.invokeMethod<void>('unlock');
  } on MissingPluginException {
    // Fall through: SystemChrome below does the same.
  }
  await SystemChrome.setPreferredOrientations(const []);
}
