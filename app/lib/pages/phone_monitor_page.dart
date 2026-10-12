import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../client_registry.dart';
import '../frame_view.dart';
import '../orientation.dart';
import '../live_typing.dart';
import '../relay_client.dart';
import '../util.dart';

/// "Phone as a second monitor": the PC spins up a virtual display, moves the
/// chosen app(s) onto it, and streams that display here. Finger touches are
/// injected on the PC as real touch input, so the PC user's own cursor never
/// jumps and (by default) their focused window is handed back after each tap.
///
/// This page talks the `phone.*` protocol the agent implements:
///   phone.status  -> {driver, enabled, monitor?, settings, windows, setup?}
///   phone.enable {size?,orientation?,scale?} / phone.disable  -> status
///   phone.settings {size?,orientation?,scale?,return_focus?}  -> status
///   phone.move {window} / phone.return {window}               -> status
///   phone.setup   -> {started:true}; progress via `phone.setup` events
///   phone.touch {action,pointer,x,y}  (fire-and-forget, normalized 0..1)
///   phone.text {text} / phone.key {key} / phone.wheel {x,y,dx,dy}
/// plus a `phone.status` event broadcast whenever anything changes.
///
/// The video itself is an ordinary JPEG/PJF1 stream (screen.start with
/// monitor:'phone'); frames arrive on [RelayClient.screenFrames] with
/// src 'screen', exactly like the PC screen view.
class PhoneMonitorPage extends StatefulWidget {
  const PhoneMonitorPage({super.key, required this.client, this.onAddApp});

  final RelayClient client;

  /// Opens the "add an app to the phone screen" flow. The integration step
  /// injects the shared window switcher here; when null the page falls back
  /// to a built-in picker over `wm.list` that calls `phone.move`.
  final Future<void> Function(BuildContext context)? onAddApp;

  @override
  State<PhoneMonitorPage> createState() => _PhoneMonitorPageState();
}

enum _DriverState { loading, installed, missing, error }

class _PhoneMonitorPageState extends State<PhoneMonitorPage> {
  // The client frames/commands flow over. Swapped for the registry's client
  // when HomePage's auto-reconnect replaces it (see ScreenPage), so the page
  // keeps working instead of staying bound to a dead client.
  late RelayClient _client = widget.client;
  StreamSubscription<RelayClient>? _registrySub;
  StreamSubscription<RelayEvent>? _eventSub;
  StreamSubscription<ScreenFrame>? _frameSub;
  StreamSubscription<bool>? _connSub;

  _DriverState _driver = _DriverState.loading;
  bool _enabled = false;
  // Settings mirror of the agent's: size (fhd/hdplus/hd), orientation
  // (portrait/landscape), scale (percent), return_focus (bool).
  Map<String, dynamic> _settings = const {
    'size': 'fhd',
    'orientation': 'landscape',
    'scale': 100,
    'return_focus': true,
  };
  List<Map<String, dynamic>> _windows = const [];
  String? _driverDetail;
  // Why the very first phone.status failed (e.g. an agent without the
  // phone-screen feature); shown instead of an endless spinner.
  String? _statusError;
  // Geometry of the streamed monitor (index + size). A change means the
  // agent re-staged it (orientation/size), so the stream is restarted.
  String? _monitorKey;

  // One-time driver install progress, driven by `phone.setup` events.
  String? _setupStage;
  String? _setupError;
  bool _setupBusy = false;
  static const _setupRunning = {
    'downloading',
    'verifying',
    'waiting_for_approval',
    'installing',
  };

  // The last frame of the phone monitor. Never cleared while open: the agent
  // skips unchanged frames, so an idle screen sends nothing and the last
  // frame must keep showing.
  late final FrameDecoder _decoder = FrameDecoder(onDecoded: _onDecoded);
  // The agent agreed to flow control (screen.start answered `ack: true`).
  bool _acks = false;
  int _frameW = 9;
  int _frameH = 16; // default portrait-ish until the first frame arrives
  Rect _imageRect = Rect.zero;
  bool _connectionLost = false;
  bool _starting = false;
  // Auto-enable runs once, the first time the page sees an installed driver
  // (on open, or right after setup); after the user turns the phone screen
  // off we don't silently turn it back on.
  bool _autoEnableDone = false;
  bool _busy = false; // an enable/disable/settings request is in flight

  // Direct multi-touch: map Flutter pointer ids to injection slots 0/1 (up
  // to two contacts); a third finger is ignored. Moves are coalesced to one
  // send per slot per ~17ms (<=60/s) so a fast drag can't flood the socket.
  final Map<int, int> _slotOf = {};
  final Map<int, Offset> _slotPos = {}; // slot -> latest normalized point
  final Map<int, Offset> _pendingMove = {}; // slot -> move not yet sent
  Timer? _moveFlush;
  static const Duration _moveInterval = Duration(milliseconds: 17);


  // Sound on phone: the phone-screen apps play HERE (silenced on the PC).
  // PCM chunks go to the native PcmPlayer; while it plays, the volume keys
  // control the phone instead of being forwarded to the PC.
  static const _audioChannel = MethodChannel('pcocket/audio');
  static const _volumeKeysChannel = MethodChannel('pcocket/volume_keys');
  bool _soundOnPhone = true;
  bool _audioOn = false;
  StreamSubscription<Uint8List>? _audioSub;

  @override
  void initState() {
    super.initState();
    // If a reconnect already replaced the client before this page opened,
    // the passed-in one is stale: bind the registry's live one right away
    // (one status request, not one per client).
    _bindClient(ClientRegistry.instance.client ?? _client);
    _registrySub = ClientRegistry.instance.onChange.listen(_onRegistryClient);
    _lockOrientation('${_settings['orientation']}');
    unawaited(_loadSoundPref());
    unawaited(_refreshStatus());
  }

  Future<void> _loadSoundPref() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final v = prefs.getBool('phone_sound');
      if (v != null && mounted) setState(() => _soundOnPhone = v);
    } catch (_) {
      // Prefs unavailable: keep the default (sound on phone).
    }
  }

  /// Asks the PC for the phone-screen apps' sound and starts playing it.
  /// Idempotent: called after every (re)started stream.
  Future<void> _startAudio() async {
    if (!_soundOnPhone || !_enabled) return;
    try {
      final resp = await _client.request('phone.audio', {'enabled': true});
      if (!mounted) return;
      await _audioChannel.invokeMethod<bool>('start', {
        'rate': (resp['rate'] as num?)?.toInt() ?? 48000,
        'channels': (resp['channels'] as num?)?.toInt() ?? 2,
      });
      unawaited(_volumeKeysChannel.invokeMethod<void>('setForwarding', false));
      _audioOn = true;
      final errors = resp['errors'];
      if (errors is List && errors.isNotEmpty && mounted) {
        showMessage(context, 'Sound on phone: ${errors.first}');
      }
    } catch (e) {
      // Older agent / no capture support: the picture still works, the
      // sound just stays on the PC.
      _audioOn = false;
    }
  }

  /// Stops the phone's playback; [tellPc] also hands the sound back to the
  /// PC (not needed when the PC already dropped it, e.g. screen turned off).
  void _stopAudio({bool tellPc = true}) {
    if (tellPc) _client.send('phone.audio', {'enabled': false});
    if (!_audioOn) return;
    _audioOn = false;
    unawaited(_audioChannel.invokeMethod<bool>('stop'));
    unawaited(_volumeKeysChannel.invokeMethod<void>('setForwarding', true));
  }

  void _onAudio(Uint8List pcm) {
    if (_audioOn) unawaited(_audioChannel.invokeMethod<void>('write', pcm));
  }

  Future<void> _toggleSound() async {
    final on = !_soundOnPhone;
    setState(() => _soundOnPhone = on);
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('phone_sound', on);
    } catch (_) {
      // Not persisted; still applied for this session.
    }
    if (on) {
      await _startAudio();
    } else {
      _stopAudio();
    }
  }

  /// The phone screen is a PC monitor: hold the page in the monitor's own
  /// orientation, full screen, so a landscape PC screen fills the phone
  /// instead of being letterboxed into a portrait phone layout.
  String? _lockedOrientation;

  void _lockOrientation(String orientation) {
    if (orientation == _lockedOrientation) return;
    _lockedOrientation = orientation;
    // Landscape follows the phone either way round, even with rotation
    // locked (see lockSensorLandscape).
    unawaited(orientation == 'portrait'
        ? SystemChrome.setPreferredOrientations(
            const [DeviceOrientation.portraitUp])
        : lockSensorLandscape());
    unawaited(SystemChrome.setEnabledSystemUIMode(SystemUiMode.immersiveSticky));
  }

  @override
  void dispose() {
    _decoder.dispose()?.dispose();
    _moveFlush?.cancel();
    _registrySub?.cancel();
    _eventSub?.cancel();
    _frameSub?.cancel();
    _connSub?.cancel();
    _audioSub?.cancel();
    _stopAudio();
    // Give the rest of the app its normal rotation and system bars back.
    unawaited(unlockOrientation());
    unawaited(SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge));
    // Stop the stream, but leave the virtual monitor enabled: the apps stay
    // on it (that is the point — a second screen), and the agent's keepalive
    // keeps it alive. "Turn off phone screen" is the explicit teardown.
    _client.send('screen.stop');
    super.dispose();
  }

  // -------------------------------------------------------------- binding

  void _bindClient(RelayClient client) {
    _eventSub?.cancel();
    _frameSub?.cancel();
    _connSub?.cancel();
    _audioSub?.cancel();
    _client = client;
    _eventSub = client.events.listen(_onEvent);
    _frameSub = client.screenFrames.listen(_onFrame);
    _audioSub = client.audioChunks.listen(_onAudio);
    _connSub = client.connectionState.listen(_onConnectionState);
  }

  void _onRegistryClient(RelayClient client) {
    if (identical(client, _client)) return;
    _bindClient(client);
    unawaited(_refreshStatus());
  }

  // -------------------------------------------------------------- status

  Future<void> _refreshStatus() async {
    try {
      final data = await _client.request('phone.status');
      if (!mounted) return;
      _applyStatus(data);
      if (!_autoEnableDone && _driver == _DriverState.installed) {
        await _ensureEnabledAndStream();
      } else if (_enabled) {
        await _startStream();
      }
    } catch (e) {
      if (!mounted) return;
      if (_driver == _DriverState.loading) {
        // Never got a status (e.g. an agent without the phone screen):
        // show it with a retry instead of spinning forever.
        setState(() {
          _driver = _DriverState.error;
          _statusError = '$e';
        });
      } else {
        showError(context, e);
      }
    }
  }

  /// Folds a `phone.status` payload (from a request reply or a broadcast
  /// event) into the page state. A broadcast can flip `enabled` or re-stage
  /// the monitor behind our back (another phone, the agent restarting), so
  /// the stream follows: started when the monitor appears or changes shape,
  /// dropped when it goes away.
  void _applyStatus(Map<String, dynamic> data) {
    final driverStr = '${data['driver'] ?? 'missing'}';
    final wasEnabled = _enabled;
    final oldMonitorKey = _monitorKey;
    setState(() {
      _driver = switch (driverStr) {
        'installed' => _DriverState.installed,
        'error' => _DriverState.error,
        _ => _DriverState.missing,
      };
      _statusError = null;
      _enabled = data['enabled'] == true;
      final s = (data['settings'] as Map?)?.cast<String, dynamic>();
      if (s != null) _settings = {..._settings, ...s};
      _lockOrientation('${_settings['orientation']}');
      _windows = ((data['windows'] as List?) ?? const [])
          .whereType<Map>()
          .map((e) => e.cast<String, dynamic>())
          .toList();
      final detail = data['driver_detail'];
      _driverDetail = detail is Map ? '${detail['state'] ?? ''}' : null;
      final mon = data['monitor'];
      _monitorKey = mon is Map
          ? '${mon['index']}:${mon['width']}x${mon['height']}'
          : null;
      final setup = data['setup'];
      if (setup is Map && !_setupBusy) {
        _setupStage = '${setup['stage'] ?? ''}';
        _setupError = setup['error']?.toString();
      }
      if (!_enabled && wasEnabled) {
        _decoder.clear();
        _stopAudio(tellPc: false);
      }
    });
    if (_enabled &&
        (!wasEnabled ||
            (oldMonitorKey != null &&
                _monitorKey != null &&
                oldMonitorKey != _monitorKey))) {
      // Idempotent: the agent replaces any old streamer server-side, and
      // _startStream's own guard drops a duplicate in flight.
      unawaited(_startStream());
    } else if (!_enabled && wasEnabled) {
      // The monitor is gone: don't leave a streamer aimed at a monitor
      // index that may now belong to another screen.
      _client.send('screen.stop');
    }
  }

  Future<void> _ensureEnabledAndStream() async {
    _autoEnableDone = true;
    if (!_enabled) {
      await _enable();
    } else {
      await _startStream();
    }
  }

  // -------------------------------------------------------------- enable

  Future<void> _enable() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      // The PC's saved settings decide the shape (a landscape 1920x1080
      // PC-style screen by default), not however the phone is held.
      final data = await _client.request('phone.enable');
      if (!mounted) return;
      _applyStatus(data);
      if (_enabled) await _startStream();
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _disable() async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      _client.send('screen.stop');
      final data = await _client.request('phone.disable');
      if (!mounted) return;
      setState(() {
        _decoder.clear();
      });
      _applyStatus(data);
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _applySettings(Map<String, dynamic> params) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final data = await _client.request('phone.settings', params);
      if (mounted) _applyStatus(data);
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // -------------------------------------------------------------- stream

  Future<void> _startStream() async {
    if (_starting || !_enabled) return;
    _starting = true;
    try {
      final resp = await _client.request('screen.start', {
        'monitor': 'phone',
        'binary': true,
        'codec': 'jpeg',
        // The one stream mode, same as the Screen page: adaptive Auto at up
        // to 60 fps (unchanged frames are never sent, so a 24 fps movie
        // costs 24 frames a second, not 60).
        'preset': 'auto',
        'fps': 60,
        // Ack every frame (see _onFrame): at most two are ever on the way,
        // so a slow link can't make the picture lag behind.
        'ack': true,
      });
      if (!mounted) return;
      setState(() {
        _acks = resp['ack'] == true;
        _connectionLost = false;
      });
      unawaited(_startAudio());
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      _starting = false;
    }
  }

  // -------------------------------------------------------------- events

  void _onEvent(RelayEvent event) {
    if (!mounted) return;
    switch (event.name) {
      case 'phone.status':
        _applyStatus(event.data);
      case 'phone.setup':
        _onSetupProgress(event.data);
      case 'agent.online':
        setState(() => _connectionLost = false);
        unawaited(_refreshStatus());
    }
  }

  void _onSetupProgress(Map<String, dynamic> data) {
    setState(() {
      _setupStage = '${data['stage'] ?? ''}';
      _setupError = data['error']?.toString();
      if (_setupStage == 'done' || _setupStage == 'error') _setupBusy = false;
    });
    if (_setupStage == 'done') {
      // The driver is in; re-query and bring the monitor up.
      _autoEnableDone = false;
      unawaited(_refreshStatus());
    }
  }

  void _onFrame(ScreenFrame frame) {
    if (!mounted || frame.src != 'screen') return;
    if (_acks) _client.send('screen.ack');
    _decoder.add(frame.jpegBytes, frame.w, frame.h);
  }

  /// Relayout only when the frame size changed or the stream is back after
  /// a drop; other frames just repaint the picture (FrameImage).
  void _onDecoded(int w, int h) {
    if (!mounted) return;
    if (w != _frameW || h != _frameH || _connectionLost) {
      setState(() {
        _frameW = w;
        _frameH = h;
        _connectionLost = false;
      });
    }
  }

  void _onConnectionState(bool connected) {
    if (!mounted) return;
    if (connected) {
      unawaited(_refreshStatus());
    } else {
      // Fingers that were down can't be lifted over a dead socket; the
      // agent cancels contacts that go quiet, so just start clean here.
      _slotOf.clear();
      _slotPos.clear();
      _pendingMove.clear();
      setState(() => _connectionLost = true);
    }
  }

  // -------------------------------------------------------------- setup

  Future<void> _startSetup() async {
    setState(() {
      _setupBusy = true;
      _setupStage = 'downloading';
      _setupError = null;
    });
    try {
      await _client.request('phone.setup');
      // Progress now arrives as `phone.setup` events handled above.
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _setupBusy = false;
        _setupError = '$e';
        _setupStage = 'error';
      });
    }
  }

  // -------------------------------------------------------------- touch

  /// Normalizes a pointer position to 0..1 inside the displayed image rect.
  /// With [inside], a point off the image (the letterbox bars) gives null
  /// instead of being clamped onto the monitor's edge.
  Offset? _normalize(Offset local, {bool inside = false}) {
    if (_imageRect.isEmpty) return null;
    if (inside && !_imageRect.contains(local)) return null;
    final x = (local.dx - _imageRect.left) / _imageRect.width;
    final y = (local.dy - _imageRect.top) / _imageRect.height;
    return Offset(x.clamp(0.0, 1.0), y.clamp(0.0, 1.0));
  }

  void _onPointerDown(PointerDownEvent e) {
    if (!_enabled) return;
    if (_slotOf.length >= 2) return; // at most two contacts
    // A finger landing on the black bars around the picture is not a touch
    // on the PC; clamping it would tap whatever sits on the monitor's edge.
    final n = _normalize(e.localPosition, inside: true);
    if (n == null) return;
    final slot = _slotOf.containsValue(0) ? 1 : 0;
    _slotOf[e.pointer] = slot;
    _slotPos[slot] = n;
    _sendTouch('down', slot, n);
  }

  void _onPointerMove(PointerMoveEvent e) {
    final slot = _slotOf[e.pointer];
    if (slot == null) return;
    final n = _normalize(e.localPosition);
    if (n == null) return;
    _slotPos[slot] = n;
    // Coalesce: keep only the latest point per slot and flush on a timer, so
    // a rapid drag sends at most ~60 moves/s instead of one per pixel.
    _pendingMove[slot] = n;
    _moveFlush ??= Timer(_moveInterval, _flushMoves);
  }

  void _flushMoves() {
    _moveFlush = null;
    if (!mounted || _pendingMove.isEmpty) return;
    final pending = Map<int, Offset>.from(_pendingMove);
    _pendingMove.clear();
    pending.forEach((slot, n) => _sendTouch('move', slot, n));
  }

  void _onPointerUp(PointerUpEvent e) => _endPointer(e.pointer, 'up');

  void _onPointerCancel(PointerCancelEvent e) => _endPointer(e.pointer, 'cancel');

  void _endPointer(int pointer, String action) {
    final slot = _slotOf.remove(pointer);
    if (slot == null) return;
    // Flush any pending move for this slot first so the up lands last.
    final pend = _pendingMove.remove(slot);
    if (pend != null) _sendTouch('move', slot, pend);
    // The lift carries the finger's last position too: every phone.touch
    // has x/y, so the agent never has to guess where a contact ended.
    _sendTouch(action, slot, _slotPos.remove(slot));
  }

  void _sendTouch(String action, int slot, Offset? n) {
    _client.send('phone.touch', {
      'action': action,
      'pointer': slot,
      if (n != null) 'x': n.dx,
      if (n != null) 'y': n.dy,
    });
  }

  // -------------------------------------------------------------- keyboard

  void _openKeyboard() {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => Padding(
        padding: EdgeInsets.only(
          left: 16,
          right: 16,
          top: 16,
          bottom: MediaQuery.of(ctx).viewInsets.bottom + 16,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Live: every keystroke, delete and Enter on the phone
            // keyboard goes straight to the PC (see LiveTypingField).
            LiveTypingField(
              label: 'Type on the phone screen',
              onText: (text) => _client.send('phone.text', {'text': text}),
              onKey: (key) => _client.send('phone.key', {'key': key}),
            ),
          ],
        ),
      ),
    );
  }

  // -------------------------------------------------------------- add app

  Future<void> _addApp() async {
    final adder = widget.onAddApp;
    if (adder != null) {
      await adder(context);
      if (mounted) unawaited(_refreshStatus());
      return;
    }
    // Built-in fallback: pick a window from wm.list and move it here.
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.6,
        builder: (ctx, scrollCtrl) => _AppPickerSheet(
          client: _client,
          scrollController: scrollCtrl,
          onMoved: (data) {
            if (mounted) _applyStatus(data);
          },
        ),
      ),
    );
  }

  Future<void> _returnApp(Object id) async {
    try {
      final data = await _client.request('phone.return', {'window': id});
      if (mounted) _applyStatus(data);
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  // -------------------------------------------------------------- menu

  void _onMenu(String value) {
    switch (value) {
      case 'orientation':
        _showOrientationSheet();
      case 'size':
        _showSizeSheet();
      case 'scale':
        _showScaleSheet();
      case 'return_focus':
        _applySettings({'return_focus': !(_settings['return_focus'] == true)});
      case 'sound':
        unawaited(_toggleSound());
      case 'off':
        _disable();
    }
  }

  void _showOrientationSheet() {
    _choose<String>(
      title: 'Orientation',
      current: '${_settings['orientation']}',
      options: const {'portrait': 'Portrait', 'landscape': 'Landscape'},
      onPick: (v) => _applySettings({'orientation': v}),
    );
  }

  void _showSizeSheet() {
    _choose<String>(
      title: 'Size',
      current: '${_settings['size']}',
      options: const {
        'fhd': '1080p (1920×1080)',
        'hdplus': '900p (1600×900)',
        'hd': '720p (1280×720)',
      },
      onPick: (v) => _applySettings({'size': v}),
    );
  }

  void _showScaleSheet() {
    final current = (_settings['scale'] as num?)?.toInt() ?? 100;
    _choose<int>(
      title: 'Text size',
      current: current,
      options: const {
        100: '100%',
        125: '125%',
        150: '150%',
        175: '175%',
        200: '200%',
      },
      onPick: (v) => _applySettings({'scale': v}),
    );
  }

  void _choose<T>({
    required String title,
    required T current,
    required Map<T, String> options,
    required void Function(T) onPick,
  }) {
    showModalBottomSheet<void>(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.all(16),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(title,
                    style: Theme.of(ctx).textTheme.titleMedium),
              ),
            ),
            for (final entry in options.entries)
              ListTile(
                title: Text(entry.value),
                trailing: entry.key == current
                    ? Icon(Icons.check,
                        color: Theme.of(ctx).colorScheme.primary)
                    : null,
                onTap: () {
                  Navigator.pop(ctx);
                  if (entry.key != current) onPick(entry.key);
                },
              ),
            const SizedBox(height: 8),
          ],
        ),
      ),
    );
  }

  // -------------------------------------------------------------- build

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Phone screen'),
        actions: _driver == _DriverState.installed && _enabled
            ? [
                IconButton(
                  tooltip: 'Add app',
                  icon: const Icon(Icons.add_to_queue),
                  onPressed: _busy ? null : () => unawaited(_addApp()),
                ),
                IconButton(
                  tooltip: 'Keyboard',
                  icon: const Icon(Icons.keyboard),
                  onPressed: _openKeyboard,
                ),
                PopupMenuButton<String>(
                  onSelected: _onMenu,
                  itemBuilder: (ctx) => [
                    const PopupMenuItem(
                        value: 'orientation', child: Text('Orientation')),
                    const PopupMenuItem(value: 'size', child: Text('Size')),
                    const PopupMenuItem(
                        value: 'scale', child: Text('Text size')),
                    CheckedPopupMenuItem(
                      value: 'sound',
                      checked: _soundOnPhone,
                      child: const Text('Sound on phone'),
                    ),
                    CheckedPopupMenuItem(
                      value: 'return_focus',
                      checked: _settings['return_focus'] == true,
                      child: const Text('Return focus to PC after taps'),
                    ),
                    const PopupMenuDivider(),
                    const PopupMenuItem(
                        value: 'off', child: Text('Turn off phone screen')),
                  ],
                ),
              ]
            : null,
      ),
      body: SafeArea(child: _body()),
    );
  }

  Widget _body() {
    switch (_driver) {
      case _DriverState.loading:
        return const Center(child: CircularProgressIndicator());
      case _DriverState.missing:
        return _setupBody();
      case _DriverState.error:
        return _errorBody();
      case _DriverState.installed:
        return _enabled ? _streamBody() : _offBody();
    }
  }

  // ---- driver missing: one-time setup ----

  Widget _setupBody() {
    final stage = _setupStage;
    final steps = <String, String>{
      'downloading': 'Downloading the display driver…',
      'verifying': 'Verifying the driver signature…',
      'waiting_for_approval': 'Approve the Windows prompt on the PC…',
      'installing': 'Installing…',
      'done': 'Done!',
      'error': 'Setup failed',
    };
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Icon(Icons.desktop_windows_outlined, size: 56),
            const SizedBox(height: 16),
            Text(
              'Use your phone as a second screen',
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 12),
            Text(
              'This needs a one-time install of a signed virtual-display '
              'driver on your PC. A Windows prompt will appear on the PC — '
              'approve it there to continue. Nothing else changes on your PC.',
              textAlign: TextAlign.center,
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(color: Theme.of(context).hintColor),
            ),
            const SizedBox(height: 24),
            // Only a stage that is actually running spins: a leftover
            // "done" with no driver (e.g. needs a restart) must still offer
            // the button instead of a spinner that never ends.
            if (_setupBusy || _setupRunning.contains(stage)) ...[
              const Center(child: CircularProgressIndicator()),
              const SizedBox(height: 12),
              Text(
                steps[stage] ?? 'Working…',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodyLarge,
              ),
            ] else ...[
              if (_setupError != null) ...[
                Text(
                  _setupError!,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
                const SizedBox(height: 12),
              ],
              FilledButton.icon(
                onPressed: _startSetup,
                icon: const Icon(Icons.download),
                label: Text(_setupError != null
                    ? 'Try again'
                    : 'Install & set up'),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _errorBody() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.warning_amber_rounded,
                size: 48, color: Theme.of(context).colorScheme.error),
            const SizedBox(height: 16),
            Text(
              _statusError != null
                  ? 'The PC did not answer the phone-screen request '
                      '($_statusError). Updating Sudo on the PC may help.'
                  : _driverDetail == 'restart_required'
                      ? 'Restart the PC to finish setting up the display '
                          'driver.'
                      : 'The display driver is installed but not working '
                          '(${_driverDetail ?? 'error'}).',
              textAlign: TextAlign.center,
              style: Theme.of(context).textTheme.bodyLarge,
            ),
            const SizedBox(height: 16),
            OutlinedButton.icon(
              onPressed: () => unawaited(_refreshStatus()),
              icon: const Icon(Icons.refresh),
              label: const Text('Check again'),
            ),
          ],
        ),
      ),
    );
  }

  // ---- installed but off ----

  Widget _offBody() {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.phonelink_erase, size: 48),
            const SizedBox(height: 16),
            Text(
              'The phone screen is off. Turning it on adds a virtual monitor '
              'to your PC and streams it here.',
              textAlign: TextAlign.center,
              style: Theme.of(context)
                  .textTheme
                  .bodyMedium
                  ?.copyWith(color: Theme.of(context).hintColor),
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: _busy ? null : () => unawaited(_enable()),
              icon: _busy
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.cast),
              label: const Text('Turn on phone screen'),
            ),
          ],
        ),
      ),
    );
  }

  // ---- live stream ----

  Widget _streamBody() {
    return Column(
      children: [
        Expanded(
          child: Container(
            color: Colors.black,
            child: LayoutBuilder(
              builder: (context, constraints) {
                final aspect = _frameW / _frameH;
                var w = constraints.maxWidth;
                var h = w / aspect;
                if (h > constraints.maxHeight) {
                  h = constraints.maxHeight;
                  w = h * aspect;
                }
                _imageRect = Rect.fromLTWH(
                  (constraints.maxWidth - w) / 2,
                  (constraints.maxHeight - h) / 2,
                  w,
                  h,
                );
                return Stack(
                  children: [
                    Positioned.fromRect(
                      rect: _imageRect,
                      child: FrameImage(_decoder),
                    ),
                    Positioned.fill(
                      child: Listener(
                        behavior: HitTestBehavior.opaque,
                        onPointerDown: _onPointerDown,
                        onPointerMove: _onPointerMove,
                        onPointerUp: _onPointerUp,
                        onPointerCancel: _onPointerCancel,
                      ),
                    ),
                    if (_connectionLost)
                      const Positioned.fill(
                        child: AbsorbPointer(
                          child: ColoredBox(
                            color: Colors.black54,
                            child: Center(
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  CircularProgressIndicator(),
                                  SizedBox(height: 12),
                                  Text('Connection lost — reconnecting…',
                                      style:
                                          TextStyle(color: Colors.white70)),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                  ],
                );
              },
            ),
          ),
        ),
        _appChips(),
      ],
    );
  }

  Widget _appChips() {
    return Container(
      width: double.infinity,
      color: Colors.black,
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
      child: _windows.isEmpty
          ? Text(
              'No apps on the phone screen yet — tap "Add app".',
              style: Theme.of(context)
                  .textTheme
                  .bodySmall
                  ?.copyWith(color: Theme.of(context).hintColor),
            )
          // One horizontally scrolling row, not a Wrap: many apps (or a
          // landscape phone) would otherwise grow the chips past the screen
          // height and overflow the column.
          : SizedBox(
              height: 48,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                itemCount: _windows.length,
                separatorBuilder: (_, _) => const SizedBox(width: 8),
                itemBuilder: (context, i) {
                  final w = _windows[i];
                  final title = '${w['title'] ?? ''}'.trim();
                  final id = w['id'];
                  return Center(
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 220),
                      child: InputChip(
                        avatar: const Icon(Icons.desktop_windows, size: 18),
                        label: Text(
                          title.isNotEmpty ? title : '${w['process'] ?? 'App'}',
                          overflow: TextOverflow.ellipsis,
                        ),
                        deleteIcon:
                            const Icon(Icons.keyboard_return, size: 18),
                        deleteButtonTooltipMessage: 'Send back to PC',
                        onDeleted: id == null
                            ? null
                            : () => unawaited(_returnApp(id as Object)),
                      ),
                    ),
                  );
                },
              ),
            ),
    );
  }
}

/// Fallback app picker: lists the PC's top-level windows (`wm.list`) and moves
/// the chosen one onto the phone screen with `phone.move`. Only used when no
/// [PhoneMonitorPage.onAddApp] was injected.
class _AppPickerSheet extends StatefulWidget {
  const _AppPickerSheet({
    required this.client,
    required this.scrollController,
    required this.onMoved,
  });

  final RelayClient client;
  final ScrollController scrollController;
  final void Function(Map<String, dynamic> status) onMoved;

  @override
  State<_AppPickerSheet> createState() => _AppPickerSheetState();
}

class _AppPickerSheetState extends State<_AppPickerSheet> {
  List<Map<String, dynamic>>? _windows;
  String? _error;
  Object? _movingId;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final data = await widget.client.request('wm.list');
      final list = ((data['windows'] as List?) ?? const [])
          .whereType<Map>()
          .map((e) => e.cast<String, dynamic>())
          .toList();
      if (!mounted) return;
      setState(() {
        _windows = list;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = '$e');
    }
  }

  Future<void> _move(Map<String, dynamic> win) async {
    final id = win['id'];
    if (id == null || _movingId != null) return;
    setState(() => _movingId = id);
    try {
      final data = await widget.client.request('phone.move', {'window': id});
      widget.onMoved(data);
      if (mounted) Navigator.pop(context);
    } catch (e) {
      if (!mounted) return;
      setState(() => _movingId = null);
      showError(context, e);
    }
  }

  @override
  Widget build(BuildContext context) {
    final wins = _windows;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              // Expanded + ellipsis: with a large system font size the
              // title would push the refresh button off a 360dp screen.
              Expanded(
                child: Text('Add an app to the phone',
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.titleMedium),
              ),
              IconButton(
                tooltip: 'Refresh',
                icon: const Icon(Icons.refresh),
                onPressed: () => unawaited(_load()),
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: _error != null
              ? Center(child: Text(_error!))
              : wins == null
                  ? const Center(child: CircularProgressIndicator())
                  : wins.isEmpty
                      ? const Center(child: Text('No open windows'))
                      : ListView.builder(
                          controller: widget.scrollController,
                          itemCount: wins.length,
                          itemBuilder: (context, i) {
                            final w = wins[i];
                            final id = w['id'];
                            final busy = _movingId != null && _movingId == id;
                            return ListTile(
                              enabled: _movingId == null,
                              leading: const Icon(Icons.window_outlined),
                              title: Text('${w['title'] ?? '?'}',
                                  overflow: TextOverflow.ellipsis),
                              subtitle: Text('${w['process'] ?? '?'}'),
                              trailing: busy
                                  ? const SizedBox(
                                      width: 20,
                                      height: 20,
                                      child: CircularProgressIndicator(
                                          strokeWidth: 2),
                                    )
                                  : const Icon(Icons.open_in_new),
                              onTap: () => unawaited(_move(w)),
                            );
                          },
                        ),
        ),
      ],
    );
  }
}
