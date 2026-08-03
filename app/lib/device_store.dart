import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

/// A saved PC: its relay WebSocket address on the LAN (`ws://<ip>:<port>`),
/// display name and, for devices paired with a code, the pairing code
/// (empty for codeless pairings).
class SavedDevice {
  SavedDevice(
      {required this.relayUrl,
      this.code = '',
      this.token = '',
      required this.name});

  final String relayUrl;
  final String code;
  final String name;

  /// Trust credential issued by the agent on pairing (`paired.token`). Sent
  /// on every future pair for instant, prompt-free reconnection; empty for
  /// devices paired before the agent supported approval (or older agents).
  final String token;

  /// Old saved entries may carry extra keys (e.g. `lanUrl`) or lack newer
  /// ones (e.g. `token`); they are simply ignored/defaulted so they still
  /// load.
  factory SavedDevice.fromJson(Map<String, dynamic> json) => SavedDevice(
        relayUrl: json['relayUrl'] as String? ?? '',
        code: json['code'] as String? ?? '',
        name: json['name'] as String? ?? '',
        token: json['token'] as String? ?? '',
      );

  Map<String, dynamic> toJson() =>
      {'relayUrl': relayUrl, 'code': code, 'name': name, 'token': token};
}

/// Persists the list of saved PCs in shared_preferences as a JSON list.
class DeviceStore {
  static const String _key = 'saved_devices';
  static const String _phoneNameKey = 'phone_name';

  /// The name this phone introduces itself with in pair requests — shown in
  /// the PC's approval dialog ("'X' wants to connect").
  static Future<String> phoneName() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString(_phoneNameKey) ?? 'My phone';
  }

  static Future<void> setPhoneName(String name) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(
        _phoneNameKey, name.trim().isEmpty ? 'My phone' : name.trim());
  }

  Future<List<SavedDevice>> load() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_key);
    if (raw == null || raw.isEmpty) return [];
    try {
      final list = jsonDecode(raw) as List;
      return list
          .map((e) => SavedDevice.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
    } catch (_) {
      return [];
    }
  }

  /// Adds or replaces the device with the same relay+code.
  Future<void> save(SavedDevice device) async {
    final devices = await load();
    devices.removeWhere(
        (d) => d.code == device.code && d.relayUrl == device.relayUrl);
    devices.add(device);
    await _persist(devices);
  }

  Future<void> delete(SavedDevice device) async {
    final devices = await load();
    devices.removeWhere(
        (d) => d.code == device.code && d.relayUrl == device.relayUrl);
    await _persist(devices);
  }

  Future<void> _persist(List<SavedDevice> devices) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(
        _key, jsonEncode(devices.map((d) => d.toJson()).toList()));
  }
}
