import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:web_socket_channel/web_socket_channel.dart';

/// An event pushed by the agent/relay without a request id
/// (e.g. `screen.frame`, `clipboard.changed`, `agent.online`, `agent.offline`).
class RelayEvent {
  RelayEvent(this.name, this.data);

  final String name;
  final Map<String, dynamic> data;
}

/// One decoded binary screen frame (`PJF1` packet: magic + little-endian
/// header length + UTF-8 JSON header + JPEG payload).
class ScreenFrame {
  ScreenFrame(this.jpegBytes, this.w, this.h, {this.src = 'screen'});

  final Uint8List jpegBytes;
  final int w;
  final int h;

  /// Frame source from the `PJF1` header: `"screen"` for the desktop
  /// capture, `"camera"` for the webcam. Older agents send no `src`, and
  /// their frames default to `'screen'`.
  final String src;
}

/// Outcome of [connectAndPair]: the `paired` handshake reply plus the
/// address that was used.
class PairResult {
  PairResult(this.paired, this.usedUrl);

  final Map<String, dynamic> paired;
  final String usedUrl;
}

/// One entry of a `{type:"choose"}` reply: an online agent the user may
/// pick to pair with.
class ChooseDevice {
  ChooseDevice(this.code, this.name);

  final String code;
  final String name;
}

/// Thrown by [RelayClient.pair] when a codeless pair matches several online
/// agents and the relay asks the caller to pick one. Retry [RelayClient.pair]
/// with the chosen [ChooseDevice.code] to finish pairing.
class ChooseDeviceException implements Exception {
  ChooseDeviceException(this.devices);

  final List<ChooseDevice> devices;

  @override
  String toString() => 'Several PCs are online; choose one to pair with';
}

/// Connects [client] to [relayUrl] (e.g. `ws://192.168.1.10:8080`) and pairs
/// it. When [code] is null or empty a codeless pair is sent (no `code`
/// field). [timeout] bounds the socket connect; the pair handshake has its
/// own reply timeout (see [RelayClient.pair]).
Future<PairResult> connectAndPair(
  RelayClient client, {
  required String relayUrl,
  String? code,
  Duration? timeout,
}) async {
  await client.connect(relayUrl, timeout: timeout);
  final paired = await client.pair(code: code);
  return PairResult(paired, relayUrl);
}

class RelayException implements Exception {
  RelayException(this.message);

  final String message;

  @override
  String toString() => message;
}

/// Wraps the WebSocket connection to the relay and speaks the Sudo
/// wire protocol: pair handshake, id-matched request/response, fire-and-forget
/// sends and pushed events.
class RelayClient {
  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  int _nextId = 0;
  bool _connected = false;

  final Map<String, Completer<Map<String, dynamic>>> _pending = {};
  final StreamController<Map<String, dynamic>> _messagesCtrl =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<RelayEvent> _eventsCtrl =
      StreamController<RelayEvent>.broadcast();
  final StreamController<ScreenFrame> _framesCtrl =
      StreamController<ScreenFrame>.broadcast();
  final StreamController<bool> _connCtrl = StreamController<bool>.broadcast();
  final StreamController<Map<String, dynamic>> _commandsCtrl =
      StreamController<Map<String, dynamic>>.broadcast();

  /// Raw incoming JSON messages (responses, events, handshake frames).
  Stream<Map<String, dynamic>> get messages => _messagesCtrl.stream;

  /// Server-pushed commands: incoming JSON messages that carry a `cmd` field
  /// but no `id` (i.e. not a response to a request), e.g. `phone.input`
  /// commands the agent fires at the phone while viewing its screen.
  Stream<Map<String, dynamic>> get commands => _commandsCtrl.stream;

  /// Pushed events (screen.frame, clipboard.changed, agent.online/offline).
  Stream<RelayEvent> get events => _eventsCtrl.stream;

  /// Binary frames (`PJF1` packets): screen captures from `screen.start`
  /// with `binary: true` and webcam captures from `camera.start`. Check
  /// [ScreenFrame.src] to route them. Frames may stop arriving while the
  /// source is idle — consumers should keep showing the last one.
  Stream<ScreenFrame> get screenFrames => _framesCtrl.stream;

  /// Emits `true` when the socket comes up, `false` when it drops.
  Stream<bool> get connectionState => _connCtrl.stream;

  bool get isConnected => _connected;

  /// Opens the WebSocket to [relayUrl] (e.g. `ws://192.168.1.10:8080`).
  /// When [timeout] is given, a socket that isn't ready in time is closed
  /// and the call fails with [RelayException].
  Future<void> connect(String relayUrl, {Duration? timeout}) async {
    await disconnect();
    final channel = WebSocketChannel.connect(Uri.parse(relayUrl));
    _channel = channel;
    try {
      await (timeout == null ? channel.ready : channel.ready.timeout(timeout));
    } catch (e) {
      _channel = null;
      try {
        // On a blackholed socket (stale IP after a network switch) an
        // unbounded close() never completes and would hang the caller
        // forever — bound it.
        await channel.sink.close().timeout(const Duration(seconds: 2));
      } catch (_) {
        // Socket already gone, or close timed out; either way, move on.
      }
      throw RelayException('Could not connect to $relayUrl');
    }
    _connected = true;
    _connCtrl.add(true);
    _sub = channel.stream.listen(
      _onMessage,
      onDone: _onSocketClosed,
      onError: (_) => _onSocketClosed(),
    );
  }

  /// Sends the pair handshake and waits for `{type:"paired"}` (resolves with
  /// the full message). When [code] is null or empty the `code` field is
  /// omitted and the relay pairs to the single online agent; if several are
  /// online it replies `{type:"choose"}` and this throws
  /// [ChooseDeviceException] with the candidates. `{type:"error"}` throws
  /// [RelayException]. Throws after [timeout] (default 20s) with no reply.
  Future<Map<String, dynamic>> pair(
      {String? code, Duration timeout = const Duration(seconds: 20)}) {
    if (!_connected) {
      return Future.error(RelayException('Not connected'));
    }
    final completer = Completer<Map<String, dynamic>>();
    late StreamSubscription<Map<String, dynamic>> msgSub;
    late StreamSubscription<bool> connSub;
    Timer? timer;

    void finish([Object? error, Map<String, dynamic>? value]) {
      timer?.cancel();
      msgSub.cancel();
      connSub.cancel();
      if (completer.isCompleted) return;
      if (error != null) {
        completer.completeError(error);
      } else {
        completer.complete(value);
      }
    }

    msgSub = messages.listen((msg) {
      final type = msg['type'];
      if (type == 'paired') {
        finish(null, msg);
      } else if (type == 'choose') {
        final devices = <ChooseDevice>[
          for (final d in (msg['devices'] as List? ?? const []))
            if (d is Map) ChooseDevice('${d['code'] ?? ''}', '${d['name'] ?? ''}'),
        ];
        finish(ChooseDeviceException(devices));
      } else if (type == 'error') {
        finish(RelayException('${msg['error'] ?? 'Pairing failed'}'));
      }
    });
    connSub = connectionState.listen((up) {
      if (!up) finish(RelayException('Connection lost while pairing'));
    });
    timer = Timer(timeout, () => finish(RelayException('Pairing timed out')));
    _write({
      'type': 'pair',
      if (code != null && code.isNotEmpty) 'code': code,
    });
    return completer.future;
  }

  /// Sends a command with an incrementing id and completes with the `data`
  /// map of the matching `{id, ok:true, data}` response. Throws
  /// [RelayException] on `ok:false` or after a 20s timeout.
  Future<Map<String, dynamic>> request(String cmd,
      [Map<String, dynamic> params = const {}]) {
    if (!_connected) {
      return Future.error(RelayException('Not connected'));
    }
    final id = 'req-${_nextId++}';
    final completer = Completer<Map<String, dynamic>>();
    _pending[id] = completer;
    final timer = Timer(const Duration(seconds: 20), () {
      final c = _pending.remove(id);
      if (c != null && !c.isCompleted) {
        c.completeError(RelayException('Request timed out: $cmd'));
      }
    });
    completer.future.whenComplete(() => timer.cancel());
    _write({'id': id, 'cmd': cmd, ...params});
    return completer.future;
  }

  /// Fire-and-forget command without an id (no response is routed back).
  void send(String cmd, [Map<String, dynamic> params = const {}]) {
    if (!_connected) return;
    _write({'cmd': cmd, ...params});
  }

  /// Sends raw bytes as a single binary WebSocket frame (no-op when
  /// disconnected, mirroring [send]). Used for `PJF1` phone-screen packets.
  void sendBytes(Uint8List data) {
    if (!_connected) return;
    _channel?.sink.add(data);
  }

  /// Closes the socket and fails all pending requests.
  Future<void> disconnect() async {
    _connected = false;
    await _sub?.cancel();
    _sub = null;
    final channel = _channel;
    _channel = null;
    if (channel != null) {
      try {
        await channel.sink.close().timeout(const Duration(seconds: 2));
      } catch (_) {
        // Socket already gone, or close timed out on a dead link.
      }
    }
    _failPending('Disconnected');
  }

  void _write(Map<String, dynamic> msg) {
    _channel?.sink.add(jsonEncode(msg));
  }

  void _onMessage(dynamic raw) {
    if (raw is! String) {
      _onBinaryMessage(raw);
      return;
    }
    Map<String, dynamic> msg;
    try {
      msg = (jsonDecode(raw) as Map).cast<String, dynamic>();
    } catch (_) {
      return; // Not JSON; ignore.
    }
    _messagesCtrl.add(msg);

    final id = msg['id'];
    if (id is String && _pending.containsKey(id)) {
      final completer = _pending.remove(id)!;
      if (msg['ok'] == true) {
        completer.complete(
            (msg['data'] as Map?)?.cast<String, dynamic>() ?? const {});
      } else {
        completer.completeError(
            RelayException('${msg['error'] ?? 'Command failed'}'));
      }
      return;
    }

    final event = msg['event'];
    if (event is String) {
      _eventsCtrl.add(RelayEvent(
          event, (msg['data'] as Map?)?.cast<String, dynamic>() ?? const {}));
      return;
    }

    // A `cmd` with no `id` is a server-pushed command, not a response
    // (responses are consumed by the pending-request block above).
    if (msg['cmd'] is String && id == null) {
      _commandsCtrl.add(msg);
      return;
    }

    final type = msg['type'];
    if (type == 'agent.online' || type == 'agent.offline') {
      _eventsCtrl.add(RelayEvent('$type', const {}));
    }
  }

  /// Handles a binary WebSocket frame. Only `PJF1` screen packets are
  /// understood; anything else is dropped.
  ///
  /// Layout: `[4B "PJF1"][4B LE uint32 headerLen][headerLen bytes UTF-8 JSON
  /// header {"w","h","src"}][JPEG bytes]`. `src` is `"screen"` or `"camera"`;
  /// a missing `src` means `"screen"` (older agents).
  void _onBinaryMessage(dynamic raw) {
    // web_socket_channel normally delivers Uint8List, but accept any
    // List<int> so a different implementation can't silently break us.
    if (raw is! List<int>) return;
    final bytes = raw is Uint8List ? raw : Uint8List.fromList(raw);
    // 0x50 'P', 0x4A 'J', 0x46 'F', 0x31 '1'.
    if (bytes.length < 8 ||
        bytes[0] != 0x50 ||
        bytes[1] != 0x4A ||
        bytes[2] != 0x46 ||
        bytes[3] != 0x31) {
      return;
    }
    final headerLen = ByteData.sublistView(bytes).getUint32(4, Endian.little);
    if (bytes.length < 8 + headerLen) return;
    try {
      final headerJson = utf8.decode(bytes.sublist(8, 8 + headerLen));
      final header = (jsonDecode(headerJson) as Map).cast<String, dynamic>();
      final w = (header['w'] as num?)?.toInt() ?? 0;
      final h = (header['h'] as num?)?.toInt() ?? 0;
      final src = header['src'] as String? ?? 'screen';
      final jpeg = Uint8List.sublistView(bytes, 8 + headerLen);
      _framesCtrl.add(ScreenFrame(jpeg, w, h, src: src));
    } catch (_) {
      // Malformed packet; drop it and wait for the next one.
    }
  }

  void _onSocketClosed() {
    if (!_connected) return;
    _connected = false;
    _failPending('Connection lost');
    _connCtrl.add(false);
  }

  void _failPending(String reason) {
    for (final completer in _pending.values) {
      if (!completer.isCompleted) {
        completer.completeError(RelayException(reason));
      }
    }
    _pending.clear();
  }
}
