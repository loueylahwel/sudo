import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../client_registry.dart';
import '../relay_client.dart';
import '../util.dart';

enum _GestureState { idle, pressing, dragging, longPressPending, dragSelect }

class ScreenPage extends StatefulWidget {
  const ScreenPage({super.key, required this.client});

  final RelayClient client;

  @override
  State<ScreenPage> createState() => _ScreenPageState();
}

class _ScreenPageState extends State<ScreenPage> {
  // The client frames/inputs currently flow over. Swapped for the registry's
  // client when HomePage's auto-reconnect replaces it, so the page keeps
  // working instead of staying bound to a dead client.
  late RelayClient _client = widget.client;
  StreamSubscription<RelayClient>? _registrySub;
  StreamSubscription<RelayEvent>? _eventSub;
  StreamSubscription<ScreenFrame>? _frameSub;
  StreamSubscription<bool>? _connSub;
  // The last frame received (binary or legacy base64). Never cleared while
  // the page is open: the agent skips unchanged frames, so an idle PC sends
  // nothing and the UI must keep showing the last frame indefinitely.
  Uint8List? _frame;
  int _frameW = 16;
  int _frameH = 9;
  Rect _imageRect = Rect.zero;
  bool _streaming = false;
  // True from socket loss until the stream is back (first frame after
  // reconnection, or `agent.online`). Drives the reconnecting overlay.
  bool _connectionLost = false;
  // Reentrancy guard for _startStream (agent.online and socket reconnect
  // can both trigger it).
  bool _starting = false;
  // True when the agent accepted `binary: true`; false = legacy base64
  // `screen.frame` events (older agents).
  bool _binaryMode = false;

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
  Offset _downPos = Offset.zero;
  Offset _lastPos = Offset.zero;
  DateTime _downTime = DateTime.now();
  Timer? _lpTimer;
  DateTime? _lastTapUp;
  static const double _slop = 8;
  static const Duration _longPressDelay = Duration(milliseconds: 350);
  static const Duration _tapMaxDuration = Duration(milliseconds: 300);
  static const Duration _doubleTapWindow = Duration(milliseconds: 300);

  final _keyboardCtrl = TextEditingController();

  @override
  void initState() {
    super.initState();
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
    _registrySub?.cancel();
    _eventSub?.cancel();
    _frameSub?.cancel();
    _connSub?.cancel();
    _keyboardCtrl.dispose();
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
    if (_starting) return; // a screen.start is already in flight
    _starting = true;
    try {
      final resp = await _client.request('screen.start',
          {'fps': 10, 'quality': 50, 'max_width': 960, 'binary': true});
      if (mounted) {
        setState(() {
          _streaming = true;
          _binaryMode = resp['binary'] == true;
        });
      }
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      _starting = false;
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
      setState(() {
        _connectionLost = true;
        _streaming = false;
      });
    }
  }

  void _onFrame(ScreenFrame frame) {
    if (!mounted) return;
    // Camera frames share the same socket stream; they belong to CameraPage
    // and must never hijack the PC screen view.
    if (frame.src != 'screen') return;
    setState(() {
      _frame = frame.jpegBytes;
      if (frame.w > 0) _frameW = frame.w;
      if (frame.h > 0) _frameH = frame.h;
      // First frame after a reconnect: the stream is back, hide the overlay.
      _connectionLost = false;
    });
  }

  void _onEvent(RelayEvent event) {
    if (!mounted) return;
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
      final bytes = base64Decode(jpeg);
      setState(() {
        _frame = bytes;
        _frameW = (event.data['w'] as num?)?.toInt() ?? _frameW;
        _frameH = (event.data['h'] as num?)?.toInt() ?? _frameH;
        // First frame after a reconnect: the stream is back.
        _connectionLost = false;
      });
    } catch (_) {
      // Corrupt frame; wait for the next one.
    }
  }

  /// Accumulates a touchpad pixel delta and schedules a single `move_rel`
  /// send on the next frame, so rapid drag events coalesce into at most one
  /// message per frame.
  void _queueMoveRel(Offset pixelDelta) {
    if (_imageRect.isEmpty || pixelDelta == Offset.zero) return;
    _pendingDx += pixelDelta.dx / _imageRect.width * _sensitivity;
    _pendingDy += pixelDelta.dy / _imageRect.height * _sensitivity;
    // A Timer, NOT addPostFrameCallback: post-frame callbacks only run when
    // the UI renders a new frame — and on an idle PC screen no new video
    // frames arrive, nothing renders, and the deltas would sit here unsent
    // (the "cursor only moves when the background moves" bug).
    _moveFlushTimer ??=
        Timer(const Duration(milliseconds: 16), _flushMoveRel);
  }

  void _flushMoveRel() {
    _moveFlushTimer = null;
    if (!mounted) return;
    final dx = _pendingDx;
    final dy = _pendingDy;
    _pendingDx = 0;
    _pendingDy = 0;
    if (dx == 0 && dy == 0) return;
    _client.send('input', {'action': 'move_rel', 'dx': dx, 'dy': dy});
  }

  /// Click at the PC cursor's current position (no x/y coordinates).
  void _click(String button, int count) {
    _client.send(
        'input', {'action': 'click', 'button': button, 'count': count});
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

  /// Two-finger drag, like a laptop touchpad: finger down = content up
  /// (wheel up), on both axes.
  void _applyTwoFingerDelta(Offset delta) {
    _scrollAccum += delta.dy;
    _scrollAccumX += delta.dx;
    while (_scrollAccum.abs() >= _scrollThreshold) {
      _scroll(0, _scrollAccum > 0 ? -1 : 1);
      _scrollAccum -= _scrollThreshold * _scrollAccum.sign;
    }
    while (_scrollAccumX.abs() >= _scrollThreshold) {
      _scroll(_scrollAccumX > 0 ? -1 : 1, 0);
      _scrollAccumX -= _scrollThreshold * _scrollAccumX.sign;
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
    // Second finger mid-gesture → laptop-style two-finger scroll.
    if (!_twoFinger && _secondPointerId == null) {
      _secondPointerId = event.pointer;
      _secondPos = event.localPosition;
      _twoFinger = true;
      _cancelLpTimer();
      _scrollAccum = 0;
      _scrollAccumX = 0;
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
      _applyTwoFingerDelta(delta);
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
            TextField(
              controller: _keyboardCtrl,
              autofocus: true,
              decoration: const InputDecoration(
                labelText: 'Type on the PC',
                border: OutlineInputBorder(),
              ),
              onSubmitted: (_) => _sendKeyboardText(),
            ),
            const SizedBox(height: 8),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton.icon(
                  onPressed: () =>
                      _client.send('input', {'action': 'key', 'key': 'backspace'}),
                  icon: const Icon(Icons.backspace_outlined),
                  label: const Text('Backspace'),
                ),
                TextButton.icon(
                  onPressed: () =>
                      _client.send('input', {'action': 'key', 'key': 'enter'}),
                  icon: const Icon(Icons.keyboard_return),
                  label: const Text('Enter'),
                ),
                FilledButton(
                  onPressed: _sendKeyboardText,
                  child: const Text('Send'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  void _sendKeyboardText() {
    final text = _keyboardCtrl.text;
    if (text.isNotEmpty) {
      _client.send('input', {'action': 'text', 'text': text});
      _keyboardCtrl.clear();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Screen'),
        actions: [
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
                Positioned.fromRect(
                  rect: _imageRect,
                  child: _frame != null
                      ? Image.memory(
                          _frame!,
                          gaplessPlayback: true,
                          fit: BoxFit.fill,
                        )
                      : Center(
                          child: Text(
                            _streaming
                                ? 'Waiting for frames…'
                                : 'Starting stream…',
                            style: const TextStyle(color: Colors.white54),
                          ),
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
                Positioned(
                  left: 0,
                  right: 0,
                  bottom: 12,
                  child: IgnorePointer(
                    child: Center(
                      child: DecoratedBox(
                        decoration: BoxDecoration(
                          color: Colors.black54,
                          borderRadius: BorderRadius.circular(20),
                        ),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 14, vertical: 6),
                          child: Text(
                            'Drag to move cursor • Two fingers to scroll • Tap to click • Hold for right-click',
                            style: const TextStyle(
                                color: Colors.white70, fontSize: 12),
                          ),
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
    );
  }
}
