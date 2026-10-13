import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// Reaching the PC through its Cloudflare quick tunnel (agent/tunnel.py)
/// when it can't be reached directly (the home provider blocks incoming
/// connections).
///
/// Each WebSocket opened on the tunnel address carries one raw TCP stream
/// into the PC's TLS port. [TunnelProxy] turns that back into a plain
/// socket on this phone's loopback, so [RelayClient] connects to
/// `wss://127.0.0.1:<port>` with the same certificate pin as on the LAN:
/// Cloudflare only ever carries encrypted bytes.
class TunnelProxy {
  TunnelProxy._();

  static final TunnelProxy instance = TunnelProxy._();

  ServerSocket? _server;
  String _target = '';

  /// The loopback `wss://` address that leads into [tunnelUrl]
  /// (`https://<name>.trycloudflare.com`). One listener is kept and reused
  /// across reconnects; a new tunnel address replaces it (connections
  /// already open keep running).
  Future<String> localUrl(String tunnelUrl) async {
    var server = _server;
    if (server == null || _target != tunnelUrl) {
      final old = server;
      _server = null;
      unawaited(old?.close());
      server = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
      _server = server;
      _target = tunnelUrl;
      final wsUrl = _wsUrl(tunnelUrl);
      final listening = server;
      listening.listen(
        (socket) => unawaited(_bridge(socket, wsUrl)),
        onDone: () {
          if (identical(_server, listening)) _server = null;
        },
        onError: (_) {
          if (identical(_server, listening)) _server = null;
        },
      );
    }
    return 'wss://127.0.0.1:${server.port}';
  }

  static String _wsUrl(String tunnelUrl) {
    final u = Uri.parse(tunnelUrl);
    return u.replace(scheme: u.scheme == 'http' ? 'ws' : 'wss', path: '/')
        .toString();
  }

  /// Pipes one local connection through a WebSocket to the tunnel. Bytes
  /// written before the WebSocket is up (the TLS hello) are held back and
  /// sent first.
  Future<void> _bridge(Socket socket, String wsUrl) async {
    socket.setOption(SocketOption.tcpNoDelay, true);
    final pending = <List<int>>[];
    WebSocket? ws;
    var closed = false;

    void closeAll() {
      if (closed) return;
      closed = true;
      socket.destroy();
      unawaited(ws?.close());
    }

    socket.listen(
      (data) {
        final w = ws;
        if (w != null) {
          w.add(data);
        } else {
          pending.add(data);
        }
      },
      onDone: closeAll,
      onError: (_) => closeAll(),
      cancelOnError: true,
    );
    final WebSocket w;
    try {
      w = await WebSocket.connect(wsUrl,
              compression: CompressionOptions.compressionOff)
          .timeout(const Duration(seconds: 10));
    } catch (_) {
      closeAll();
      return;
    }
    if (closed) {
      unawaited(w.close());
      return;
    }
    // Cloudflare drops a WebSocket idle for ~100 s; the PC's own pings
    // inside already keep it busy, this covers the gaps.
    w.pingInterval = const Duration(seconds: 30);
    ws = w;
    for (final chunk in pending) {
      w.add(chunk);
    }
    pending.clear();
    w.listen(
      (message) {
        if (message is List<int>) {
          socket.add(message);
        }
      },
      onDone: closeAll,
      onError: (_) => closeAll(),
      cancelOnError: true,
    );
  }
}

/// The tunnel address the PC last posted to its secret ntfy topic
/// ([rendezvous] = `https://ntfy.sh/sudo-<random>`), or null when none can
/// be read. Only a hint: whatever it says, the connection through it must
/// still pass the PC's certificate pin.
Future<String?> latestTunnelUrl(String rendezvous) async {
  final client = HttpClient()..connectionTimeout = const Duration(seconds: 6);
  try {
    final request = await client
        .getUrl(Uri.parse('$rendezvous/json?poll=1&since=all'))
        .timeout(const Duration(seconds: 6));
    final response = await request.close().timeout(const Duration(seconds: 6));
    if (response.statusCode != 200) return null;
    final body = await response
        .transform(utf8.decoder)
        .join()
        .timeout(const Duration(seconds: 6));
    String? url;
    for (final line in const LineSplitter().convert(body)) {
      try {
        final event = jsonDecode(line);
        if (event is! Map || event['event'] != 'message') continue;
        final message = jsonDecode('${event['message']}');
        final u = message is Map ? '${message['url'] ?? ''}' : '';
        if (u.startsWith('https://')) url = u; // the newest is last
      } catch (_) {
        // Not ours (or not JSON): skip.
      }
    }
    return url;
  } catch (_) {
    return null;
  } finally {
    client.close(force: true);
  }
}
