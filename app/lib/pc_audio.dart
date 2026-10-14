import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'client_registry.dart';
import 'relay_client.dart';

/// "PC sound on this phone": everything the PC plays comes out of the phone
/// instead, at home and away (the packets ride the same socket as
/// everything else, so the Cloudflare tunnel carries them too).
///
/// Protocol (agent side in `agent/pc_audio.py`):
///   audio.start {mute_pc}  -> {enabled, codec:"opus", rate, channels, ...}
///   audio.stop             -> {enabled:false}  (sound back on the PC)
///   audio.status           -> {enabled, mute_pc, mine}
/// plus `OPS1` binary packets (one 20 ms Opus packet each) and an
/// `audio.stopped` event when another phone takes the sound over.
///
/// Independent of `phone.audio` (the phone-screen apps' sound, PCM1), which
/// the phone screen page drives on its own.
///
/// One app-wide instance, because the sound must outlive the widgets: it
/// keeps playing while the Screen or Files page covers the dashboard and
/// while HomePage's auto-reconnect swaps the client underneath (followed
/// through [ClientRegistry.onChange]). The switch always starts off when
/// the app starts and is never persisted: a PC going quiet by itself after
/// the app was reopened would only confuse.
class PcAudio extends ChangeNotifier {
  PcAudio._() {
    _native.setMethodCallHandler(_onNativeCall);
    // Never cancelled: the instance lives as long as the app.
    ClientRegistry.instance.onChange.listen(_onClient);
    final current = ClientRegistry.instance.client;
    if (current != null) _bind(current);
  }

  static final PcAudio instance = PcAudio._();

  // Native side: OpusPlayer (MediaCodec + AudioTrack) and the foreground
  // service that keeps it playing with the screen off. Dart -> native:
  // start / write(bytes) / stop. Native -> Dart: stopped(reason), when the
  // phone ended it by itself (notification's Stop, another app took the
  // audio focus, decoder failure).
  static const _native = MethodChannel('sudo/pcaudio');

  RelayClient? _client;
  StreamSubscription<OpusPacket>? _packetSub;
  StreamSubscription<RelayEvent>? _eventSub;
  StreamSubscription<bool>? _connSub;

  bool _enabled = false;
  bool _playing = false;
  bool _starting = false;
  bool _nativeOn = false;
  String? _error;
  bool? _supported;
  bool _elsewhere = false;

  // Heartbeat while the switch is on (`audio.alive` every 5 s). The agent
  // drops a listener that then says nothing for 30 s: a phone that walked
  // off the Wi-Fi leaves no trace on its socket, and without this the PC
  // would stay muted until TCP gave up, minutes later.
  Timer? _alive;

  // Packets that came off the socket while on, and those handed to the
  // native player: with its own counters (`stats`) they ride along with the
  // heartbeat, and the PC's log shows where the sound gets stuck.
  int _rx = 0;
  int _fwd = 0;

  // Bumped by every start and stop: an `audio.start` reply that arrives
  // after the user flipped the switch again (or after a newer start on a
  // newer client) must not touch the state any more.
  int _gen = 0;

  final StreamController<String> _notices =
      StreamController<String>.broadcast();

  /// The user's switch. Stays on across reconnects: the next client picks
  /// the sound up again by itself.
  bool get enabled => _enabled;

  /// The PC agreed and its sound is flowing to this phone right now (false
  /// while starting and while the connection is down).
  bool get playing => _playing;

  /// An `audio.start` is in flight.
  bool get starting => _starting;

  /// Why the last start failed (the agent's own words, e.g. a missing
  /// package on the PC); cleared by the next flip of the switch.
  String? get error => _error;

  /// False when the connected agent predates this feature (it answered
  /// "unknown cmd"); null until it has been asked.
  bool? get supported => _supported;

  /// Another phone has the PC's sound right now (from `audio.status`, only
  /// tracked while this phone's switch is off).
  bool get elsewhere => _elsewhere;

  /// One-line notes for a snackbar: the sound stopped without the user
  /// touching the switch (another phone took over, another app on this
  /// phone started playing, the notification's Stop).
  Stream<String> get notices => _notices.stream;

  /// Flips the switch. On: start the phone's player, then ask the PC for
  /// its sound. Off: hand the sound back to the PC and stop the player.
  Future<void> setEnabled(bool on, {String reason = 'switched off'}) async {
    if (on == _enabled) return;
    _error = null;
    if (on) {
      _enabled = true;
      _elsewhere = false;
      _rx = 0;
      _fwd = 0;
      _alive ??= Timer.periodic(
          const Duration(seconds: 5), (_) => unawaited(_heartbeat()));
      notifyListeners();
      await _start();
    } else {
      _stop(tellPc: true, reason: reason);
    }
  }

  /// audio.alive, with what the phone's player is doing.
  Future<void> _heartbeat() async {
    final client = _client;
    if (!_enabled || client == null || !client.isConnected) return;
    Map<String, dynamic> stats = {};
    try {
      final native = await _native.invokeMethod<Map>('stats');
      if (native != null) stats = native.cast<String, dynamic>();
    } catch (e) {
      stats = {'stats_error': '$e'};
    }
    stats['rx'] = _rx;
    stats['fwd'] = _fwd;
    stats['dart_playing'] = _playing;
    if (identical(client, _client) && client.isConnected) {
      client.send('audio.alive', {'stats': stats});
    }
  }

  // ------------------------------------------------------------- binding

  void _bind(RelayClient client) {
    _packetSub?.cancel();
    _eventSub?.cancel();
    _connSub?.cancel();
    _client = client;
    _packetSub = client.opusPackets.listen(_onPacket);
    _eventSub = client.events.listen(_onEvent);
    _connSub = client.connectionState.listen(_onConnection);
  }

  /// HomePage published a new client (a fresh connect or an automatic
  /// reconnect). With the switch on, ask this one for the sound again: the
  /// agent kept the PC's sound routed for a grace period after the old
  /// socket died, so it simply carries on (the native player kept running
  /// meanwhile and just rebuffers).
  void _onClient(RelayClient client) {
    if (identical(client, _client)) return;
    _bind(client);
    _playing = false;
    if (_enabled) {
      unawaited(_start());
    } else {
      unawaited(_refreshStatus());
    }
    notifyListeners();
  }

  void _onConnection(bool up) {
    if (up || !_playing) return;
    // Keep the switch and the native player as they are: the agent holds
    // the sound for a while and the next client asks for it again.
    _playing = false;
    notifyListeners();
  }

  void _onPacket(OpusPacket packet) {
    if (_enabled) _rx++;
    if (!_enabled || !_nativeOn) return;
    _fwd++;
    unawaited(_native
        .invokeMethod<void>('write', packet.data)
        .then((_) {}, onError: (_) {}));
  }

  void _onEvent(RelayEvent event) {
    if (event.name != 'audio.stopped') return;
    // Another phone took the sound over (or the PC ended it). This phone
    // is no longer the listener, so no audio.stop: it would cut the sound
    // away from the phone that has it now.
    final reason = '${event.data['reason'] ?? ''}'.trim();
    if (_enabled) {
      _stop(tellPc: false);
      _notices.add(reason.isEmpty
          ? 'The PC sound stopped playing here'
          : 'The PC sound stopped playing here: $reason');
    }
    unawaited(_refreshStatus());
  }

  Future<dynamic> _onNativeCall(MethodCall call) async {
    if (call.method != 'stopped') return null;
    // The phone ended it (notification's Stop, another app took the audio
    // focus, decoder failure): give the sound back to the PC.
    final reason = call.arguments is String ? call.arguments as String : '';
    _nativeOn = false;
    if (_enabled) {
      _stop(tellPc: true, reason: 'phone: $reason');
      if (reason.isNotEmpty) _notices.add(reason);
    }
    return null;
  }

  // ---------------------------------------------------------- start/stop

  Future<void> _start() async {
    final client = _client;
    if (!_enabled || client == null || !client.isConnected) return;
    final gen = ++_gen;
    _starting = true;
    notifyListeners();
    try {
      // The player first, so the first packets after the reply already
      // have somewhere to go. It stays up across reconnects (only the
      // switch going off stops it).
      if (!_nativeOn) {
        await _native.invokeMethod<bool>('start');
        // Switched off (or restarted for a newer client) meanwhile: channel
        // calls run in order, so the stop _stop sent already ran after this
        // start, or the newer start owns the player now.
        if (gen != _gen) return;
        _nativeOn = true;
      }
      final reply = await client.request('audio.start', {'mute_pc': true});
      if (gen != _gen || !_enabled) return;
      final codec = '${reply['codec'] ?? 'opus'}';
      if (codec != 'opus') {
        _fail(client, "The PC sends its sound as '$codec', which this app "
            "can't play. Update the app.");
        return;
      }
      _supported = true;
      _playing = identical(client, _client) && client.isConnected;
    } on PlatformException catch (e) {
      // The phone can't play right now (a call holds the audio, no Opus
      // decoder): nothing was asked of the PC yet.
      if (gen != _gen) return;
      _fail(null, e.message ?? "The phone can't play the PC sound right now");
    } on MissingPluginException {
      if (gen != _gen) return;
      _fail(null, "This phone can't play the PC sound");
    } catch (e) {
      if (gen != _gen || !_enabled) return;
      if (!identical(client, _client) || !client.isConnected) {
        // The link dropped mid-request: not an answer. Keep the switch on,
        // the next client asks again.
        return;
      }
      if (_isUnknownCmd(e)) {
        // An agent from before this feature: nothing to stop on the PC.
        _supported = false;
        _fail(null, 'The Sudo agent on this PC is too old for this. '
            'Update it on the PC.');
      } else {
        _fail(client, '$e');
      }
    } finally {
      if (gen == _gen) {
        _starting = false;
        notifyListeners();
      }
    }
  }

  /// A start that failed: switch off, show [message] under the switch.
  /// [client] (when the PC was asked) also gets an audio.stop, because a
  /// start that timed out may still have gone through on the PC and left
  /// its speakers muted.
  void _fail(RelayClient? client, String message) {
    _stop(tellPc: client != null, reason: 'failed: $message');
    _error = message;
    notifyListeners();
  }

  /// Switch off: [tellPc] hands the sound back to the PC (best effort; if
  /// the link is down, the agent restores it by itself after its grace
  /// period).
  void _stop({required bool tellPc, String reason = ''}) {
    _gen++;
    _enabled = false;
    _alive?.cancel();
    _alive = null;
    _playing = false;
    _starting = false;
    final client = _client;
    if (tellPc && client != null && client.isConnected) {
      unawaited(client
          .request('audio.stop', {'reason': reason})
          .then((_) => true, onError: (_) => false));
    }
    _nativeOn = false;
    unawaited(_stopNative());
    notifyListeners();
  }

  Future<void> _stopNative() =>
      _native.invokeMethod<void>('stop').then((_) {}, onError: (_) {});

  /// After a (re)connect with the switch off: is the agent new enough, and
  /// does another phone have the sound right now?
  Future<void> _refreshStatus() async {
    final client = _client;
    if (client == null || !client.isConnected) return;
    try {
      final status = await client.request('audio.status');
      if (!identical(client, _client)) return;
      _supported = true;
      _elsewhere =
          !_enabled && status['enabled'] == true && status['mine'] != true;
    } catch (e) {
      if (!identical(client, _client)) return;
      if (_isUnknownCmd(e)) _supported = false;
      _elsewhere = false;
    }
    notifyListeners();
  }

  bool _isUnknownCmd(Object e) => '$e'.startsWith('unknown cmd');
}
