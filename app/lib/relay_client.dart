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

/// One `H264` binary packet: the JSON header (carries `w`, `h`, `codec`,
/// `encoder`, `fps`) plus the raw H.264 Annex-B bytestream chunk that
/// followed it. Chunks are arbitrary splits of one continuous bytestream
/// (SPS/PPS lead it); they must be fed, in order, to the platform decoder.
class VideoChunk {
  VideoChunk(this.header, this.data);

  final Map<String, dynamic> header;
  final Uint8List data;
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
/// field). [token] is the trust credential issued on an earlier pairing;
/// when given it is sent along and the pair completes without an on-PC
/// approval prompt. [timeout] bounds the socket connect; the pair handshake
/// has its own reply timeout (see [RelayClient.pair]). [onApprovalPending]
/// fires when the relay asks the user to approve the connection on the PC
/// (see [RelayClient.pair]).
Future<PairResult> connectAndPair(
  RelayClient client, {
  required String relayUrl,
  String? code,
  String? token,
  String? name,
  Duration? timeout,
  void Function()? onApprovalPending,
}) async {
  await client.connect(relayUrl, timeout: timeout);
  final paired = await client.pair(
      code: code,
      token: token,
      name: name,
      onApprovalPending: onApprovalPending);
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
  final StreamController<VideoChunk> _videoChunksCtrl =
      StreamController<VideoChunk>.broadcast();
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

  /// Binary `H264` packets from `screen.start` with `codec: 'h264'`: raw
  /// Annex-B bytestream chunks for the hardware decoder, in wire order.
  Stream<VideoChunk> get videoChunks => _videoChunksCtrl.stream;

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
  /// [RelayException] (e.g. `denied on this PC` when the user rejects the
  /// approval dialog on the PC). Throws after [timeout] (default 20s) with
  /// no reply.
  ///
  /// [token] is the trust credential a newer agent issued on an earlier
  /// pairing (the `token` field of `paired`); when given it is sent along
  /// and the pair completes instantly, without an approval prompt.
  ///
  /// Agents with `require_approval` answer an unknown phone with
  /// `{type:"approval.pending"}` and show an Allow/Deny dialog on the PC
  /// (30s auto-deny) before the final `paired`/`error`. That message is
  /// neither success nor failure: [onApprovalPending] is called once and
  /// the wait continues with the timeout extended to 60s so the user has
  /// time to react on the PC.
  Future<Map<String, dynamic>> pair(
      {String? code,
      String? token,
      String? name,
      Duration timeout = const Duration(seconds: 20),
      void Function()? onApprovalPending}) {
    if (!_connected) {
      return Future.error(RelayException('Not connected'));
    }
    final completer = Completer<Map<String, dynamic>>();
    late StreamSubscription<Map<String, dynamic>> msgSub;
    late StreamSubscription<bool> connSub;
    Timer? timer;
    var approvalPending = false;

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
      } else if (type == 'approval.pending') {
        // The PC is showing an Allow/Deny dialog (30s auto-deny). Surface
        // the pending state and keep waiting for the final paired/error,
        // with the reply timeout extended to 60s so the user has time to
        // react on the PC.
        if (!approvalPending) {
          approvalPending = true;
          onApprovalPending?.call();
          timer?.cancel();
          timer = Timer(const Duration(seconds: 60),
              () => finish(RelayException('Pairing timed out')));
        }
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
      if (token != null && token.isNotEmpty) 'token': token,
      if (name != null && name.isNotEmpty) 'name': name,
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

  /// Handles a binary WebSocket frame. `PJF1` (JPEG screen/camera) and
  /// `H264` (hardware-decoded screen video) packets are understood;
  /// anything else is dropped.
  ///
  /// `PJF1` layout: `[4B "PJF1"][4B LE uint32 headerLen][headerLen bytes
  /// UTF-8 JSON header {"w","h","src"}][JPEG bytes]`. `src` is `"screen"`
  /// or `"camera"`; a missing `src` means `"screen"` (older agents).
  ///
  /// `H264` layout: `[4B "H264"][4B LE uint32 headerLen][headerLen bytes
  /// UTF-8 JSON header {"w","h","codec","encoder","fps"}][raw Annex-B
  /// bytestream chunk]`.
  void _onBinaryMessage(dynamic raw) {
    // web_socket_channel normally delivers Uint8List, but accept any
    // List<int> so a different implementation can't silently break us.
    if (raw is! List<int>) return;
    final bytes = raw is Uint8List ? raw : Uint8List.fromList(raw);
    if (bytes.length < 8) return;
    // 0x48 'H', 0x32 '2', 0x36 '6', 0x34 '4'.
    if (bytes[0] == 0x48 &&
        bytes[1] == 0x32 &&
        bytes[2] == 0x36 &&
        bytes[3] == 0x34) {
      _onH264Packet(bytes);
      return;
    }
    // 0x50 'P', 0x4A 'J', 0x46 'F', 0x31 '1'.
    if (bytes[0] != 0x50 ||
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

  /// Parses one `H264` packet (see [_onBinaryMessage] for the layout) and
  /// emits its header + bytestream chunk on [videoChunks].
  void _onH264Packet(Uint8List bytes) {
    final headerLen = ByteData.sublistView(bytes).getUint32(4, Endian.little);
    if (bytes.length < 8 + headerLen) return;
    try {
      final headerJson = utf8.decode(bytes.sublist(8, 8 + headerLen));
      final header = (jsonDecode(headerJson) as Map).cast<String, dynamic>();
      final data = Uint8List.sublistView(bytes, 8 + headerLen);
      if (data.isEmpty) return;
      _videoChunksCtrl.add(VideoChunk(header, data));
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
