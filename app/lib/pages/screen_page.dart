import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;

import '../client_registry.dart';
import '../frame_view.dart';
import '../orientation.dart';
import '../live_typing.dart';
import '../relay_client.dart';
import '../util.dart';
import 'window_switcher.dart';

enum _GestureState { idle, pressing, dragging, longPressPending, dragSelect }

enum _TwoFingerMode { pinch, scroll }

/// The one stream mode: JPEG, the agent's adaptive `auto` preset (width and
/// quality step down when Wi-Fi backs up) at up to 60 fps. Agents that cap
/// fps lower simply send fewer frames.
///
/// `ack`: this app acks every frame it receives (`screen.ack`), and the
/// agent keeps only as many frames unacked as the link's round trip needs
/// — so frames can never queue up on a slow or bursty link (the picture
/// lagging behind the cursor).
///
/// `cursor: 'client'`: this app draws the PC pointer itself (see
/// [_ScreenPageState._localCursor]); agents that can't answer
/// `cursor: 'frame'` and paint it into the frames as before.
const Map<String, dynamic> _streamParams = {
  'codec': 'jpeg',
  'preset': 'auto',
  'fps': 60,
  'binary': true,
  'ack': true,
  'cursor': 'client',
};

/// What this app sent before presets existed, for agents that ignore
/// `preset` (they answer without one).
const Map<String, dynamic> _legacyStreamParams = {
  'codec': 'jpeg',
  'fps': 15,
  'quality': 65,
  'max_width': 1152,
  'binary': true,
};

class ScreenPage extends StatefulWidget {
  const ScreenPage({super.key, required this.client});

  final RelayClient client;

  @override
  State<ScreenPage> createState() => _ScreenPageState();
}

class _ScreenPageState extends State<ScreenPage>
    with SingleTickerProviderStateMixin {
  // The client frames/inputs currently flow over. Swapped for the registry's
  // client when HomePage's auto-reconnect replaces it, so the page keeps
  // working instead of staying bound to a dead client.
  late RelayClient _client = widget.client;

  // Window lock (window.lock): the PC's current window stays on top and
  // new windows open underneath it. null until the agent answered (false:
  // an agent or desktop without it, the button is hidden).
  bool? _lockSupported;
  bool _locked = false;
  String _lockTitle = '';
  bool _lockBusy = false;
  StreamSubscription<RelayClient>? _registrySub;
  StreamSubscription<RelayEvent>? _eventSub;
  StreamSubscription<ScreenFrame>? _frameSub;
  StreamSubscription<bool>? _connSub;
  // The last decoded frame. Never cleared while the page is open: the agent
  // skips unchanged frames, so an idle PC sends nothing and the UI must keep
  // showing the last frame indefinitely.
  late final FrameDecoder _decoder = FrameDecoder(onDecoded: _onDecoded);
  // The last frame and its size when a Screen page closed, shown the
  // moment the next one opens — the PC screen appears at once instead of a
  // blank page, and live frames replace it within a fraction of a second.
  static ui.Image? _cachedImage;
  static int _cachedW = 16;
  static int _cachedH = 9;
  // The agent agreed to flow control (screen.start answered `ack: true`).
  bool _acks = false;
  int _frameW = 16;
  int _frameH = 9;
  Rect _imageRect = Rect.zero;
  // True from socket loss until the stream is back (first frame after
  // reconnection, or `agent.online`). Drives the reconnecting overlay.
  bool _connectionLost = false;
  // Reentrancy guard for _startStream (agent.online and socket reconnect
  // can both trigger it).
  bool _starting = false;
  // True when the agent accepted `binary: true`; false = legacy base64
  // `screen.frame` events (older agents).
  bool _binaryMode = false;

  // Set when screen.start answered without `preset`: the agent predates
  // presets and fell back to its own low defaults, so requests carry the
  // explicit settings this app always sent instead.
  bool _legacyAgent = false;
  // A restart requested while a screen.start was in flight;
  // _startStream runs again once the current one finishes.
  bool _restartPending = false;

  double _scrollAccum = 0;
  double _scrollAccumX = 0;
  static const double _scrollThreshold = 24;

  // Touchpad movement: pixel deltas are normalized by the displayed image
  // size, scaled by _sensitivity and coalesced into one `move_rel` send per
  // frame (see _queueMoveRel).
  double _pendingDx = 0;
  double _pendingDy = 0;
  Timer? _moveFlushTimer;
  static const double _sensitivity = 1.5;

  // Client-drawn pointer (screen.start answered `cursor: 'client'`): drawn
  // here at the PC's last reported position plus the moves the PC hasn't
  // applied yet, so it follows the finger at once however slow the link
  // (over the internet a frame takes 100-300 ms to come back). Every
  // move_rel carries a `seq`; `cursor` events report the position with
  // the last seq applied, and the moves up to it are dropped.
  bool _localCursor = false;
  int _moveSeq = 0;
  Offset? _cursorBase; // PC-reported position, fraction of the monitor
  final List<(int, double, double)> _unappliedMoves = [];
  final ValueNotifier<Offset?> _cursorPos = ValueNotifier(null);

  // Gesture state machine, driven by raw Listener pointer events (no gesture
  // arena, so a slow micro-drag can never be hijacked by the long-press
  // recognizer). States:
  //   idle             no finger down
  //   pressing         finger down, long-press timer armed, still under slop
  //   dragging         moved past slop before the timer fired (touchpad/scroll)
  //   longPressPending timer fired while under slop, finger still down
  //   dragSelect       long-press followed by a move past slop (button held)
  _GestureState _gesture = _GestureState.idle;
  int? _pointerId; // the finger currently driving the gesture
  // Laptop-style touchpad: a second finger switches the gesture to scroll.
  int? _secondPointerId;
  Offset _secondPos = Offset.zero;
  bool _twoFinger = false;
  // Pinch-to-zoom: two-finger distance scales the view, anchored at the
  // pinch midpoint. One finger = cursor, two-finger translate = scroll.
  double _zoomScale = 1.0;
  Offset _zoomOrigin = Offset.zero;
  double _pinchStartDist = 0;
  double _pinchStartScale = 1.0;
  Offset _pinchStartMid = Offset.zero;
  Offset _pinchImageFrac = Offset.zero; // image fraction under the midpoint
  // Two-finger intent: pinch vs scroll is decided once per gesture after a
  // small deadband — fingers moving apart/together = pinch, moving in the
  // same direction = scroll. Never both at once (that was the confusion).
  _TwoFingerMode? _twoFingerMode;
  Offset _downPos = Offset.zero;
  Offset _lastPos = Offset.zero;
  DateTime _downTime = DateTime.now();
  Timer? _lpTimer;
  DateTime? _lastTapUp;
  static const double _slop = 8;
  static const Duration _longPressDelay = Duration(milliseconds: 350);
  static const Duration _tapMaxDuration = Duration(milliseconds: 300);
  static const Duration _doubleTapWindow = Duration(milliseconds: 300);

  // Voice dictation: hold the app-bar mic to talk. speech_to_text requests
  // the RECORD_AUDIO runtime permission itself inside initialize(). Only
  // FINAL results are typed at the PC cursor; partials just render in the
  // "Listening…" overlay. _pulseCtrl drives the red pulsing mic icon.
  final stt.SpeechToText _speech = stt.SpeechToText();
  bool _speechAvailable = false;
  bool _listening = false;
  String _dictationText = '';
  late final AnimationController _pulseCtrl = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  );

  // App dock along the bottom edge (running PC apps, tap to switch). The
  // key lets the page re-list right after the switcher sheet closes.
  final _dockKey = GlobalKey<AppDockState>();
  // The gesture cheat-sheet only shows for the first seconds, then fades
  // out — permanently, it just covered the PC screen.
  bool _showGestureHint = true;
  Timer? _gestureHintTimer;

  // Full screen: the PC screen alone — no app bar, no dock, no system bars.
  // Automatic whenever the phone is landscape; the app-bar button forces
  // it (and landscape) from portrait, and Back leaves it.
  bool _fullscreenForced = false;
  bool? _immersive; // the system-bar mode last applied

  @override
  void initState() {
    super.initState();
    final cached = _cachedImage;
    if (cached != null) {
      _decoder.image.value = cached.clone();
      _frameW = _cachedW;
      _frameH = _cachedH;
    }
    _gestureHintTimer = Timer(const Duration(seconds: 4), () {
      if (mounted) setState(() => _showGestureHint = false);
    });
    _bindClient(_client);
    _registrySub = ClientRegistry.instance.onChange.listen(_onRegistryClient);
    // If a reconnect already replaced the client before this page opened,
    // the passed-in one is stale: switch to the registry's live one.
    final current = ClientRegistry.instance.client;
    if (current != null) _onRegistryClient(current);
    _startStream();
  }

  @override
  void dispose() {
    _lpTimer?.cancel();
    _moveFlushTimer?.cancel();
    _gestureHintTimer?.cancel();
    if (_fullscreenForced) unawaited(unlockOrientation());
    if (_immersive == true) {
      unawaited(SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge));
    }
    _registrySub?.cancel();
    _eventSub?.cancel();
    _frameSub?.cancel();
    _connSub?.cancel();
    // Keep the last frame for the next Screen page (see _cachedImage).
    final last = _decoder.dispose();
    if (last != null) {
      _cachedImage?.dispose();
      _cachedImage = last;
      _cachedW = _frameW;
      _cachedH = _frameH;
    }
    _pulseCtrl.dispose();
    _cursorPos.dispose();
    unawaited(_speech.stop());
    _client.send('screen.stop');
    super.dispose();
  }

  /// Points the event/frame/connection subscriptions at [client].
  void _bindClient(RelayClient client) {
    _eventSub?.cancel();
    _frameSub?.cancel();
    _connSub?.cancel();
    _client = client;
    _eventSub = client.events.listen(_onEvent);
    _frameSub = client.screenFrames.listen(_onFrame);
    unawaited(_refreshLock(client));
    _connSub = client.connectionState.listen(_onConnectionState);
  }

  /// Handles a replacement client from the registry (auto-reconnect). The
  /// new client's socket is already up, so the stream is restarted right
  /// away; the relay's agent.online re-push triggers _startStream again,
  /// which is idempotent.
  void _onRegistryClient(RelayClient client) {
    if (identical(client, _client)) return;
    _bindClient(client);
    _startStream();
  }

  Future<void> _startStream() async {
    if (_starting) {
      // A screen.start is already in flight; run once more after it.
      _restartPending = true;
      return;
    }
    _starting = true;
    try {
      final resp = await _client.request(
        'screen.start',
        _legacyAgent ? _legacyStreamParams : _streamParams,
      );
      if (!mounted) return;
      if (resp['preset'] == null && !_legacyAgent) {
        // The agent predates presets: it ignored `preset` and fell back to
        // its own low defaults. Ask again with the explicit settings.
        _legacyAgent = true;
        _restartPending = true;
      }
      setState(() {
        _binaryMode = resp['binary'] == true;
        _acks = resp['ack'] == true;
        _localCursor = resp['cursor'] == 'client';
      });
      // A new stream: moves sent before it were applied or lost with the
      // old connection; the reply's pointer is the fresh starting point.
      _unappliedMoves.clear();
      _cursorBase = null;
      final pointer = resp['pointer'];
      if (pointer is Map) _onCursor(pointer.cast<String, dynamic>());
      _updateLocalCursor();
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      _starting = false;
      if (_restartPending && mounted) {
        _restartPending = false;
        unawaited(_startStream());
      }
    }
  }

  void _onConnectionState(bool connected) {
    if (!mounted) return;
    if (connected) {
      // Socket is back. If the agent never went offline the relay won't
      // re-push agent.online, so restart the stream here too (idempotent:
      // the agent replaces any old streamer server-side).
      _startStream();
    } else {
      setState(() => _connectionLost = true);
    }
  }

  void _onFrame(ScreenFrame frame) {
    if (!mounted) return;
    // Camera frames share the same socket stream; they belong to CameraPage
    // and must never hijack the PC screen view.
    if (frame.src != 'screen') return;
    // Ack on arrival: the agent sends the next frame once this one is
    // here, so at most two are ever on the way. Decoding is newest-wins
    // (FrameDecoder), so a slow decode can't build a queue either.
    if (_acks) _client.send('screen.ack');
    _decoder.add(frame.jpegBytes, frame.w, frame.h);
  }

  /// A frame was decoded: relayout only when its size changed (the auto
  /// ladder stepped) or it is the first after a reconnect — every other
  /// frame just repaints the picture.
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

  Future<void> _refreshLock(RelayClient client) async {
    try {
      final r = await client.request('window.lock.status');
      if (!mounted || !identical(client, _client)) return;
      setState(() {
        _lockSupported = true;
        _applyLock(r);
      });
    } catch (e) {
      if (!mounted || !identical(client, _client)) return;
      if ('$e'.startsWith('unknown cmd')) {
        setState(() => _lockSupported = false);
      }
    }
  }

  void _applyLock(Map<String, dynamic> r) {
    _locked = r['locked'] == true;
    _lockTitle = '${r['title'] ?? ''}';
  }

  Future<void> _toggleLock() async {
    if (_lockBusy) return;
    final on = !_locked;
    setState(() => _lockBusy = true);
    try {
      final r = await _client.request('window.lock', {'on': on});
      if (!mounted) return;
      setState(() => _applyLock(r));
      showMessage(
          context,
          _locked
              ? 'Locked on top: '
                  '${_lockTitle.isEmpty ? 'this window' : _lockTitle}'
              : 'Window lock off');
    } catch (e) {
      if (!mounted) return;
      showError(
          context,
          '$e'.startsWith('unknown cmd')
              ? 'Update the Sudo agent on the PC for the window lock'
              : e);
    } finally {
      if (mounted) setState(() => _lockBusy = false);
    }
  }

  Widget _lockIcon() => Icon(
        _locked ? Icons.lock : Icons.lock_open,
        color: _locked ? Colors.amberAccent : null,
      );

  void _onEvent(RelayEvent event) {
    if (!mounted) return;
    if (event.name == 'window.lock') {
      setState(() {
        _lockSupported = true;
        _applyLock(event.data);
      });
      return;
    }
    if (event.name == 'cursor') {
      _onCursor(event.data);
      return;
    }
    if (event.name == 'agent.online') {
      // Agent (re)connected: hide the overlay and restart the stream.
      // Idempotent — the agent replaces any old streamer server-side.
      setState(() => _connectionLost = false);
      _startStream();
      return;
    }
    // Legacy base64 path, only for agents that answered `binary: false`.
    if (event.name != 'screen.frame' || _binaryMode) return;
    final jpeg = event.data['jpeg'];
    if (jpeg is! String) return;
    try {
      _decoder.add(
        base64Decode(jpeg),
        (event.data['w'] as num?)?.toInt() ?? 0,
        (event.data['h'] as num?)?.toInt() ?? 0,
      );
    } catch (_) {
      // Corrupt frame; wait for the next one.
    }
  }

  /// The PC reported its pointer (`cursor` event or the screen.start
  /// reply): the new base, minus the moves it has already applied.
  void _onCursor(Map<String, dynamic> data) {
    final x = (data['x'] as num?)?.toDouble();
    final y = (data['y'] as num?)?.toDouble();
    if (x == null || y == null) return;
    final seq = (data['seq'] as num?)?.toInt() ?? 0;
    _cursorBase = Offset(x, y);
    _unappliedMoves.removeWhere((m) => m.$1 <= seq);
    _updateLocalCursor();
  }

  /// Where to draw the pointer: the PC's position with every move it
  /// hasn't applied yet (sent or still queued) on top. Steps clamp to the
  /// screen edge like the PC's pointer does; off this monitor it's hidden.
  void _updateLocalCursor() {
    final base = _cursorBase;
    if (!_localCursor || base == null) {
      _cursorPos.value = null;
      return;
    }
    // A pointer on another monitor isn't held at this one's edges.
    final inside =
        base.dx >= 0 && base.dx <= 1 && base.dy >= 0 && base.dy <= 1;
    double step(double v, double d) =>
        inside ? (v + d).clamp(0.0, 1.0).toDouble() : v + d;
    var x = base.dx;
    var y = base.dy;
    for (final m in _unappliedMoves) {
      x = step(x, m.$2);
      y = step(y, m.$3);
    }
    x = step(x, _pendingDx);
    y = step(y, _pendingDy);
    _cursorPos.value = x >= 0 && x <= 1 && y >= 0 && y <= 1
        ? Offset(x, y)
        : null;
  }

  /// Accumulates a touchpad pixel delta and schedules a single `move_rel`
  /// send on the next frame, so rapid drag events coalesce into at most one
  /// message per frame.
  void _queueMoveRel(Offset pixelDelta) {
    if (_imageRect.isEmpty || pixelDelta == Offset.zero) return;
    _pendingDx += pixelDelta.dx / _imageRect.width * _sensitivity;
    _pendingDy += pixelDelta.dy / _imageRect.height * _sensitivity;
    // The drawn pointer moves now; the PC catches up when the send lands.
    if (_localCursor) _updateLocalCursor();
    // A Timer, NOT addPostFrameCallback: post-frame callbacks only run when
    // the UI renders a new frame — and on an idle PC screen no new video
    // frames arrive, nothing renders, and the deltas would sit here unsent
    // (the "cursor only moves when the background moves" bug).
    _moveFlushTimer ??= Timer(const Duration(milliseconds: 16), _flushMoveRel);
  }

  void _flushMoveRel() {
    _moveFlushTimer = null;
    if (!mounted) return;
    final dx = _pendingDx;
    final dy = _pendingDy;
    _pendingDx = 0;
    _pendingDy = 0;
    if (dx == 0 && dy == 0) return;
    final seq = ++_moveSeq;
    _client.send('input', {
      'action': 'move_rel',
      'dx': dx,
      'dy': dy,
      'seq': seq,
    });
    if (_localCursor) _unappliedMoves.add((seq, dx, dy));
  }

  /// Click at the PC cursor's current position (no x/y coordinates).
  void _click(String button, int count) {
    _client.send('input', {
      'action': 'click',
      'button': button,
      'count': count,
    });
  }

  void _button(String action) {
    _client.send('input', {'action': action, 'button': 'left'});
  }

  void _scroll(double dx, double dy) {
    _client.send('input', {'action': 'scroll', 'dx': dx, 'dy': dy});
  }

  /// Per-move delta while dragging with one finger: queue normalized
  /// `move_rel` deltas (touchpad cursor movement).
  void _applyDragDelta(Offset delta) {
    _queueMoveRel(delta);
  }

  /// Two-finger drag, natural scrolling (content follows the fingers, like
  /// a Windows touchpad): finger down = content down, on both axes.
  void _applyTwoFingerDelta(Offset delta) {
    _scrollAccum += delta.dy;
    _scrollAccumX += delta.dx;
    while (_scrollAccum.abs() >= _scrollThreshold) {
      _scroll(0, _scrollAccum > 0 ? 1 : -1);
      _scrollAccum -= _scrollThreshold * _scrollAccum.sign;
    }
    while (_scrollAccumX.abs() >= _scrollThreshold) {
      _scroll(_scrollAccumX > 0 ? 1 : -1, 0);
      _scrollAccumX -= _scrollThreshold * _scrollAccumX.sign;
    }
  }

  /// Recomputes zoom from the current finger distance, keeping the image
  /// point under the pinch midpoint stationary.
  void _pinchUpdate() {
    if (_imageRect.isEmpty || _pinchStartDist < 1) return;
    final mid = (_lastPos + _secondPos) / 2;
    final dist = (_secondPos - _lastPos).distance;
    final scale = (_pinchStartScale * dist / _pinchStartDist).clamp(1.0, 5.0);
    setState(() {
      _zoomScale = scale.toDouble();
      _zoomOrigin =
          mid -
          Offset(
            _pinchImageFrac.dx * _imageRect.width * _zoomScale,
            _pinchImageFrac.dy * _imageRect.height * _zoomScale,
          );
    });
  }

  /// Decides once per two-finger gesture whether it's a pinch or a scroll:
  /// compare how much the finger DISTANCE changed vs how much the MIDPOINT
  /// moved, after a small deadband that eats contact jitter. Re-anchors the
  /// pinch reference on decision so the deadband itself causes no zoom jump.
  void _classifyTwoFinger() {
    if (_twoFingerMode != null || _secondPointerId == null) return;
    final dist = (_secondPos - _lastPos).distance;
    final mid = (_lastPos + _secondPos) / 2;
    final dDist = (dist - _pinchStartDist).abs();
    final dMid = (mid - _pinchStartMid).distance;
    const deadband = 10.0;
    if (dDist < deadband && dMid < deadband) return;
    if (dDist > dMid) {
      _twoFingerMode = _TwoFingerMode.pinch;
      // Re-anchor: pinch math starts from NOW.
      _pinchStartDist = dist;
      _pinchStartMid = mid;
      _pinchStartScale = _zoomScale;
      if (!_imageRect.isEmpty) {
        final base = _zoomScale > 1.0 ? _zoomOrigin : _imageRect.topLeft;
        _pinchImageFrac = Offset(
          ((mid.dx - base.dx) / (_imageRect.width * _zoomScale)).clamp(
            0.0,
            1.0,
          ),
          ((mid.dy - base.dy) / (_imageRect.height * _zoomScale)).clamp(
            0.0,
            1.0,
          ),
        );
      }
    } else {
      _twoFingerMode = _TwoFingerMode.scroll;
      _scrollAccum = 0;
      _scrollAccumX = 0;
    }
  }

  void _onPointerDown(PointerDownEvent event) {
    // A new touch while idle always takes control. Fingers that went down
    // mid-gesture (a resting palm) are simply ignored, so they can never
    // lock out a deliberate touch.
    if (_gesture == _GestureState.idle) {
      _pointerId = event.pointer;
      _gesture = _GestureState.pressing;
      _downPos = event.localPosition;
      _lastPos = event.localPosition;
      _downTime = DateTime.now();
      _lpTimer = Timer(_longPressDelay, _onLongPressTimer);
      return;
    }
    // Second finger mid-gesture → laptop-style two-finger scroll + pinch.
    if (!_twoFinger && _secondPointerId == null) {
      _secondPointerId = event.pointer;
      _secondPos = event.localPosition;
      _twoFinger = true;
      _twoFingerMode = null; // intent decided after the deadband
      _cancelLpTimer();
      _scrollAccum = 0;
      _scrollAccumX = 0;
      // Record the pinch anchor — distance and midpoint at gesture start.
      if (!_imageRect.isEmpty) {
        final mid = (_lastPos + _secondPos) / 2;
        _pinchStartDist = (_secondPos - _lastPos).distance;
        _pinchStartMid = mid;
        _pinchStartScale = _zoomScale;
        final base = _zoomScale > 1.0 ? _zoomOrigin : _imageRect.topLeft;
        _pinchImageFrac = Offset(
          ((mid.dx - base.dx) / (_imageRect.width * _zoomScale)).clamp(
            0.0,
            1.0,
          ),
          ((mid.dy - base.dy) / (_imageRect.height * _zoomScale)).clamp(
            0.0,
            1.0,
          ),
        );
      }
      // A not-yet-committed primary can't turn into tap/long-press anymore.
      if (_gesture == _GestureState.pressing ||
          _gesture == _GestureState.longPressPending) {
        _gesture = _GestureState.dragging;
      }
    }
  }

  void _onLongPressTimer() {
    // Fired while still under slop (moving past slop cancels the timer):
    // the hold becomes a pending long-press.
    if (_gesture == _GestureState.pressing) {
      _gesture = _GestureState.longPressPending;
    }
  }

  void _onPointerMove(PointerMoveEvent event) {
    if (_twoFinger) {
      Offset delta;
      if (event.pointer == _pointerId) {
        delta = event.localPosition - _lastPos;
        _lastPos = event.localPosition;
      } else if (event.pointer == _secondPointerId) {
        delta = event.localPosition - _secondPos;
        _secondPos = event.localPosition;
      } else {
        return;
      }
      _classifyTwoFinger();
      if (_twoFingerMode == _TwoFingerMode.pinch) {
        _pinchUpdate();
      } else if (_twoFingerMode == _TwoFingerMode.scroll) {
        _applyTwoFingerDelta(delta);
      }
      // Undecided yet (inside the deadband): swallow the motion so a pinch
      // can't leak into scroll and vice versa.
      return;
    }
    if (event.pointer != _pointerId) return;
    final pos = event.localPosition;
    switch (_gesture) {
      case _GestureState.pressing:
        if ((pos - _downPos).distance > _slop) {
          _cancelLpTimer();
          _gesture = _GestureState.dragging;
          _scrollAccum = 0;
          // Apply the full displacement so the initial sub-slop movement
          // is not lost.
          _applyDragDelta(pos - _downPos);
        }
      case _GestureState.longPressPending:
        if ((pos - _downPos).distance > _slop) {
          _gesture = _GestureState.dragSelect;
          _button('down');
          _queueMoveRel(pos - _downPos);
        }
      case _GestureState.dragging:
        _applyDragDelta(pos - _lastPos);
      case _GestureState.dragSelect:
        _queueMoveRel(pos - _lastPos);
      case _GestureState.idle:
        break;
    }
    _lastPos = pos;
  }

  void _onPointerUp(PointerUpEvent event) {
    if (_twoFinger) {
      _endTwoFinger(event.pointer);
      return;
    }
    if (event.pointer != _pointerId) return;
    _cancelLpTimer();
    switch (_gesture) {
      case _GestureState.pressing:
        if (DateTime.now().difference(_downTime) <= _tapMaxDuration) {
          _onTap();
        }
      // Longer holds that never reached the long-press timer do nothing.
      case _GestureState.longPressPending:
        _click('right', 1);
      case _GestureState.dragSelect:
        _button('up');
      case _GestureState.dragging:
      case _GestureState.idle:
        break;
    }
    _resetGesture();
  }

  void _onPointerCancel(PointerCancelEvent event) {
    if (_twoFinger) {
      _endTwoFinger(event.pointer);
      return;
    }
    if (event.pointer != _pointerId) return;
    _cancelLpTimer();
    // Up-without-action: only release the button if a drag-select held it.
    if (_gesture == _GestureState.dragSelect) _button('up');
    _resetGesture();
  }

  /// One of the two fingers lifted during a two-finger scroll: the gesture
  /// continues with the remaining finger (rebaselined, so no cursor jump).
  void _endTwoFinger(int pointer) {
    _twoFingerMode = null;
    if (pointer == _secondPointerId) {
      _secondPointerId = null;
      _twoFinger = false;
      // Continue with the primary finger: a still-under-slop press becomes
      // a fresh press (a clean release can still tap), anything else stays
      // a drag with the baseline already tracked in _lastPos.
      if (_gesture == _GestureState.pressing ||
          _gesture == _GestureState.longPressPending) {
        _gesture = _GestureState.pressing;
        _downPos = _lastPos;
        _downTime = DateTime.now();
      }
    } else if (pointer == _pointerId) {
      // Primary lifted: the second finger takes over as primary.
      _pointerId = _secondPointerId;
      _secondPointerId = null;
      _twoFinger = false;
      _lastPos = _secondPos;
      _downPos = _secondPos;
      _gesture = _GestureState.dragging;
    }
  }

  /// A quick release under slop. The first tap clicks immediately (no wait
  /// for the double-tap window); a second tap released within
  /// [_doubleTapWindow] of the first sends a double-click instead.
  void _onTap() {
    final now = DateTime.now();
    final last = _lastTapUp;
    if (last != null && now.difference(last) <= _doubleTapWindow) {
      _click('left', 2);
      _lastTapUp = null; // a third rapid tap starts a new chain
    } else {
      _click('left', 1);
      _lastTapUp = now;
    }
  }

  void _cancelLpTimer() {
    _lpTimer?.cancel();
    _lpTimer = null;
  }

  void _resetGesture() {
    _gesture = _GestureState.idle;
    _pointerId = null;
  }

  Future<void> _showWindowSwitcher() async {
    await showWindowSwitcher(context, _client);
    // A switch or launch just changed the PC's windows: update the dock
    // now instead of on its next periodic refresh.
    unawaited(_dockKey.currentState?.refresh());
  }

  void _enterFullscreen() {
    setState(() => _fullscreenForced = true);
    // Either way round, following the phone (even with rotation locked).
    unawaited(lockSensorLandscape());
  }

  void _exitFullscreen() {
    setState(() => _fullscreenForced = false);
    // Back to the system's rotation: held sideways, it stays full screen.
    unawaited(unlockOrientation());
  }

  /// Hides the status and navigation bars in full screen (a swipe from the
  /// edge brings them back for a moment), restores them otherwise.
  void _syncSystemBars(bool fullscreen) {
    if (_immersive == fullscreen) return;
    _immersive = fullscreen;
    unawaited(
      SystemChrome.setEnabledSystemUIMode(
        fullscreen ? SystemUiMode.immersiveSticky : SystemUiMode.edgeToEdge,
      ),
    );
  }

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
              label: 'Type on the PC',
              onText: (text) =>
                  _client.send('input', {'action': 'text', 'text': text}),
              onKey: (key) =>
                  _client.send('input', {'action': 'key', 'key': key}),
            ),
          ],
        ),
      ),
    );
  }

  /// Hold-to-talk start. The first call initializes speech_to_text, which
  /// asks for the mic permission; a denial ends the gesture with a snackbar.
  Future<void> _startDictation() async {
    if (_listening) return;
    if (!_speechAvailable) {
      try {
        _speechAvailable = await _speech.initialize(
          onError: (_) => _endDictationUi(),
        );
      } catch (_) {
        _speechAvailable = false;
      }
      if (!_speechAvailable) {
        if (mounted) {
          showMessage(context, 'Microphone permission needed for dictation');
        }
        return;
      }
    }
    if (!mounted) return;
    setState(() {
      _listening = true;
      _dictationText = '';
    });
    _pulseCtrl.repeat(reverse: true);
    unawaited(
      _speech.listen(
        onResult: (result) {
          if (!mounted) return;
          setState(() => _dictationText = result.recognizedWords);
          // Partials only update the overlay; the FINAL result is typed at
          // the PC cursor (trailing space separates consecutive utterances).
          if (result.finalResult && result.recognizedWords.isNotEmpty) {
            _client.send('input', {
              'action': 'text',
              'text': '${result.recognizedWords} ',
            });
          }
        },
        // Device default locale (localeId omitted), partial results on.
        listenOptions: stt.SpeechListenOptions(partialResults: true),
      ),
    );
  }

  /// Hold-to-talk release. stop() still lets the in-flight utterance deliver
  /// its final result to onResult, so the last words are not lost.
  Future<void> _stopDictation() async {
    if (!_listening) return;
    _endDictationUi();
    await _speech.stop();
  }

  /// Drops the dictation UI state (used on release and on recognizer
  /// errors). The overlay only renders while _listening, so clearing the
  /// text here never hides a final result that arrives just after.
  void _endDictationUi() {
    _pulseCtrl
      ..stop()
      ..reset();
    if (mounted) {
      setState(() {
        _listening = false;
        _dictationText = '';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final fullscreen =
        _fullscreenForced ||
        MediaQuery.orientationOf(context) == Orientation.landscape;
    WidgetsBinding.instance.addPostFrameCallback(
      (_) => _syncSystemBars(fullscreen),
    );
    // In forced full screen, Back leaves full screen instead of the page.
    return PopScope(
      canPop: !_fullscreenForced,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _exitFullscreen();
      },
      child: Scaffold(
        appBar: fullscreen
            ? null
            : AppBar(
                title: const Text('Screen'),
                actions: [
                  // Hold-to-talk dictation. A GestureDetector (not IconButton) so
                  // long-press start/end drive listen/stop.
                  Tooltip(
                    message: 'Hold to dictate',
                    child: GestureDetector(
                      behavior: HitTestBehavior.opaque,
                      onLongPressStart: (_) => unawaited(_startDictation()),
                      onLongPressEnd: (_) => unawaited(_stopDictation()),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        child: Icon(
                          _listening ? Icons.mic : Icons.mic_none,
                          color: _listening ? Colors.redAccent : null,
                        ),
                      ),
                    ),
                  ),
                  if (_lockSupported == true)
                    IconButton(
                      tooltip: _locked
                          ? 'Unlock: windows may open on top again'
                          : 'Lock this window on top: new windows open '
                              'underneath',
                      icon: _lockIcon(),
                      onPressed: _lockBusy ? null : _toggleLock,
                    ),
                  IconButton(
                    tooltip: 'Full screen',
                    icon: const Icon(Icons.fullscreen),
                    onPressed: _enterFullscreen,
                  ),
                  IconButton(
                    tooltip: 'Switch app',
                    icon: const Icon(Icons.apps),
                    onPressed: _showWindowSwitcher,
                  ),
                  IconButton(
                    tooltip: 'Keyboard',
                    icon: const Icon(Icons.keyboard),
                    onPressed: _openKeyboard,
                  ),
                ],
              ),
        body: Container(
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
                  Positioned(
                    left: _zoomScale > 1.0 ? _zoomOrigin.dx : _imageRect.left,
                    top: _zoomScale > 1.0 ? _zoomOrigin.dy : _imageRect.top,
                    width: _imageRect.width * _zoomScale,
                    height: _imageRect.height * _zoomScale,
                    child: Stack(
                      fit: StackFit.expand,
                      children: [
                        FrameImage(_decoder),
                        // Repaints on its own when the pointer moves:
                        // never a rebuild of the page or the picture.
                        if (_localCursor)
                          IgnorePointer(
                            child: RepaintBoundary(
                              child: CustomPaint(
                                painter: _PointerPainter(_cursorPos),
                              ),
                            ),
                          ),
                      ],
                    ),
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
                    Positioned.fill(
                      child: AbsorbPointer(
                        child: Container(
                          color: Colors.black54,
                          child: const Center(
                            child: Column(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                CircularProgressIndicator(),
                                SizedBox(height: 12),
                                Text(
                                  'Connection lost — reconnecting…',
                                  style: TextStyle(color: Colors.white70),
                                ),
                              ],
                            ),
                          ),
                        ),
                      ),
                    ),
                  // Full screen has no app bar: the lock gets a small
                  // button of its own in the corner.
                  if (fullscreen && _lockSupported == true)
                    Positioned(
                      top: 8 + MediaQuery.paddingOf(context).top,
                      right: 8,
                      child: Material(
                        color: Colors.black45,
                        shape: const CircleBorder(),
                        child: IconButton(
                          tooltip: _locked ? 'Unlock window' : 'Lock window',
                          color: Colors.white70,
                          icon: _lockIcon(),
                          onPressed: _lockBusy ? null : _toggleLock,
                        ),
                      ),
                    ),
                  if (_zoomScale > 1.01)
                    Positioned(
                      right: 12,
                      // Clear of the app dock (none in full screen).
                      bottom: fullscreen
                          ? 12
                          : 72 + MediaQuery.paddingOf(context).bottom,
                      child: ActionChip(
                        label: Text('${(_zoomScale * 100).round()}% — reset'),
                        onPressed: () => setState(() => _zoomScale = 1.0),
                      ),
                    ),
                  // Dictation overlay: red pulsing mic + live partial text.
                  // IgnorePointer keeps every gesture flowing to the Listener.
                  if (_listening)
                    Positioned(
                      left: 24,
                      right: 24,
                      top: 16,
                      child: IgnorePointer(
                        child: Center(
                          child: DecoratedBox(
                            decoration: BoxDecoration(
                              color: Colors.black87,
                              borderRadius: BorderRadius.circular(20),
                            ),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 16,
                                vertical: 10,
                              ),
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Row(
                                    mainAxisSize: MainAxisSize.min,
                                    children: [
                                      ScaleTransition(
                                        scale: Tween(begin: 1.0, end: 1.3)
                                            .animate(
                                              CurvedAnimation(
                                                parent: _pulseCtrl,
                                                curve: Curves.easeInOut,
                                              ),
                                            ),
                                        child: const Icon(
                                          Icons.mic,
                                          color: Colors.redAccent,
                                          size: 18,
                                        ),
                                      ),
                                      const SizedBox(width: 8),
                                      const Text(
                                        'Listening…',
                                        style: TextStyle(color: Colors.white70),
                                      ),
                                    ],
                                  ),
                                  if (_dictationText.isNotEmpty) ...[
                                    const SizedBox(height: 6),
                                    Text(
                                      _dictationText,
                                      textAlign: TextAlign.center,
                                      maxLines: 3,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        color: Colors.white,
                                        fontSize: 13,
                                      ),
                                    ),
                                  ],
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  // Gesture cheat-sheet: first seconds only, then fades out.
                  if (!fullscreen)
                    Positioned(
                      left: 16,
                      right: 16,
                      bottom: 72 + MediaQuery.paddingOf(context).bottom,
                      child: IgnorePointer(
                        child: AnimatedOpacity(
                          opacity: _showGestureHint ? 1 : 0,
                          duration: const Duration(milliseconds: 600),
                          child: Center(
                            child: DecoratedBox(
                              decoration: BoxDecoration(
                                color: Colors.black54,
                                borderRadius: BorderRadius.circular(20),
                              ),
                              child: const Padding(
                                padding: EdgeInsets.symmetric(
                                  horizontal: 14,
                                  vertical: 6,
                                ),
                                child: Text(
                                  'Drag: cursor • Two fingers: scroll • Pinch: zoom • Tap: click',
                                  textAlign: TextAlign.center,
                                  style: TextStyle(
                                    color: Colors.white70,
                                    fontSize: 12,
                                  ),
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  // App dock. Above the touchpad Listener in the stack, but
                  // only its own surface takes touches: everything around it
                  // still drives the PC cursor. Lifted by the bottom system
                  // inset: with Android 15 edge-to-edge the body runs under
                  // the navigation bar, where taps belong to the system.
                  if (!_connectionLost && !fullscreen)
                    Positioned(
                      left: 8,
                      right: 8,
                      bottom: 8 + MediaQuery.paddingOf(context).bottom,
                      child: AppDock(
                        key: _dockKey,
                        client: _client,
                        onOpenSwitcher: _showWindowSwitcher,
                      ),
                    ),
                ],
              );
            },
          ),
        ),
      ),
    );
  }
}

/// The PC pointer drawn by the phone (client-drawn cursor): the same arrow
/// the agent paints into frames otherwise, tip at [position] (a fraction of
/// the picture), scaled with the picture.
class _PointerPainter extends CustomPainter {
  _PointerPainter(this.position) : super(repaint: position);

  final ValueNotifier<Offset?> position;

  @override
  void paint(Canvas canvas, Size size) {
    final p = position.value;
    if (p == null || size.isEmpty) return;
    final x = p.dx * size.width;
    final y = p.dy * size.height;
    final s = math.max(11.0, size.width / 90);
    final path = Path()
      ..moveTo(x, y)
      ..lineTo(x, y + s * 1.4)
      ..lineTo(x + s * 0.38, y + s * 1.05)
      ..lineTo(x + s * 0.58, y + s * 1.5)
      ..lineTo(x + s * 0.78, y + s * 1.42)
      ..lineTo(x + s * 0.58, y + s * 0.98)
      ..lineTo(x + s * 0.95, y + s * 0.95)
      ..close();
    canvas.drawPath(path, Paint()..color = Colors.white);
    canvas.drawPath(
      path,
      Paint()
        ..color = Colors.black
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1,
    );
  }

  @override
  bool shouldRepaint(_PointerPainter oldDelegate) =>
      oldDelegate.position != position;
}
