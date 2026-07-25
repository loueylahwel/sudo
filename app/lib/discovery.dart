import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// A PC found on the local network via the Sudo agent's UDP broadcast.
class DiscoveredPc {
  DiscoveredPc({required this.name, required this.ip, required this.port});

  /// The PC's hostname as announced by the agent (e.g. `DESKTOP-6SKHKTA`).
  final String name;

  /// Sender IP of the broadcast packet.
  final String ip;

  /// Port the agent's relay WebSocket listens on.
  final int port;

  /// WebSocket relay URL of this PC (`ws://<ip>:<port>`).
  String get relayUrl => 'ws://$ip:$port';
}

/// Listens for the PC agent's UDP broadcasts for [duration] and returns the
/// PCs that announced themselves, deduplicated by sender IP.
///
/// The agent broadcasts `SUDO|<pc-name>|<relay-port>` (plain UTF-8) to
/// 255.255.255.255:[port] every few seconds. Receiving broadcasts needs no
/// extra Android permission. Returns an empty list when the socket cannot be
/// opened or nothing answers in time; the socket is always closed.
Future<List<DiscoveredPc>> discoverPcs({
  int port = 47809,
  Duration duration = const Duration(seconds: 4),
}) async {
  RawDatagramSocket socket;
  try {
    socket = await RawDatagramSocket.bind(
      InternetAddress.anyIPv4,
      port,
      reuseAddress: true,
      reusePort: true,
    );
  } catch (_) {
    return [];
  }
  final found = <String, DiscoveredPc>{};
  final sub = socket.listen((event) {
    if (event != RawSocketEvent.read) return;
    final dg = socket.receive();
    if (dg == null) return;
    final String text;
    try {
      text = utf8.decode(dg.data);
    } catch (_) {
      return;
    }
    final parts = text.split('|');
    if (parts.length != 3 || parts[0] != 'SUDO') return;
    final relayPort = int.tryParse(parts[2].trim());
    if (relayPort == null) return;
    final ip = dg.address.address;
    found[ip] = DiscoveredPc(name: parts[1], ip: ip, port: relayPort);
  });
  await Future<void>.delayed(duration);
  await sub.cancel();
  socket.close();
  return found.values.toList();
}
