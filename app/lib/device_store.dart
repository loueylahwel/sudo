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
      this.remoteUrl = '',
      this.certSha256 = '',
      this.tlsPort = 0,
      this.tunnelUrl = '',
      this.rendezvous = '',
      required this.name});

  final String relayUrl;
  final String code;
  final String name;

  /// Trust credential issued by the agent on pairing (`paired.token`). Sent
  /// on every future pair for instant, prompt-free reconnection; empty for
  /// devices paired before the agent supported approval (or older agents).
  final String token;

  /// The PC's TLS port and the SHA-256 of its certificate (pinned), learned
  /// from `remote.info` on the first connection. Once known, every
  /// connection — LAN included — goes through that port, so the token never
  /// travels in clear text again. Kept until the PC is paired anew.
  final String certSha256;
  final int tlsPort;

  /// How to reach the PC from outside the home network
  /// (`wss://<public ip>:<tlsPort>`); empty until the PC's router forward
  /// works.
  final String remoteUrl;

  /// The PC's Cloudflare tunnel (`https://<name>.trycloudflare.com`, see
  /// tunnel.dart): works from any network, even when the home provider
  /// blocks incoming connections. It changes when the PC restarts; the
  /// current one is always posted to [rendezvous], the PC's secret ntfy
  /// topic (`https://ntfy.sh/sudo-<random>`).
  final String tunnelUrl;
  final String rendezvous;

  bool get hasPin => certSha256.length == 64 && tlsPort > 0;
  bool get hasRemote => hasPin && remoteUrl.isNotEmpty;

  /// Reachable through the tunnel: a known address, or a topic to read the
  /// current one from. Never without the pin.
  bool get hasTunnel =>
      hasPin && (tunnelUrl.isNotEmpty || rendezvous.isNotEmpty);
  bool get hasInternet => hasRemote || hasTunnel;

  SavedDevice copyWith(
          {String? relayUrl,
          String? token,
          String? remoteUrl,
          String? certSha256,
          int? tlsPort,
          String? tunnelUrl,
          String? rendezvous}) =>
      SavedDevice(
          relayUrl: relayUrl ?? this.relayUrl,
          code: code,
          name: name,
          token: token ?? this.token,
          remoteUrl: remoteUrl ?? this.remoteUrl,
          certSha256: certSha256 ?? this.certSha256,
          tlsPort: tlsPort ?? this.tlsPort,
          tunnelUrl: tunnelUrl ?? this.tunnelUrl,
          rendezvous: rendezvous ?? this.rendezvous);

  /// Old saved entries may carry extra keys (e.g. `lanUrl`) or lack newer
  /// ones (e.g. `token`); they are simply ignored/defaulted so they still
  /// load.
  factory SavedDevice.fromJson(Map<String, dynamic> json) => SavedDevice(
        relayUrl: json['relayUrl'] as String? ?? '',
        code: json['code'] as String? ?? '',
        name: json['name'] as String? ?? '',
        token: json['token'] as String? ?? '',
        remoteUrl: json['remoteUrl'] as String? ?? '',
        certSha256: json['certSha256'] as String? ?? '',
        tlsPort: (json['tlsPort'] as num?)?.toInt() ?? 0,
        tunnelUrl: json['tunnelUrl'] as String? ?? '',
        rendezvous: json['rendezvous'] as String? ?? '',
      );

  Map<String, dynamic> toJson() => {
        'relayUrl': relayUrl,
        'code': code,
        'name': name,
        'token': token,
        'remoteUrl': remoteUrl,
        'certSha256': certSha256,
        'tlsPort': tlsPort,
        'tunnelUrl': tunnelUrl,
        'rendezvous': rendezvous,
      };
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
