import 'dart:async';

import 'package:connectivity_plus/connectivity_plus.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../client_registry.dart';
import '../device_store.dart';
import '../discovery.dart';
import '../pc_widget.dart';
import '../relay_client.dart';
import '../util.dart';
import 'camera_page.dart';
import 'files_page.dart';
import 'screen_page.dart';

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final _store = DeviceStore();
  List<SavedDevice> _devices = [];
  bool _loadingDevices = true;

  SavedDevice? _selected;
  RelayClient? _client;
  StreamSubscription<RelayEvent>? _eventSub;
  StreamSubscription<bool>? _connSub;
  bool _connecting = false;
  // True while the agent waits for the user to Allow this phone in the
  // Allow/Deny dialog shown on the PC (approval.pending during pairing).
  bool _approvalPending = false;
  bool _socketUp = false;
  bool _agentOnline = true;

  Map<String, dynamic>? _sysInfo;
  bool _sysInfoLoading = false;

  // Which address the current connection ended up using.
  String? _viaText;

  // One-shot: auto-connect to the most recently saved PC on first load.
  bool _autoConnectPending = true;

  // Auto-reconnect after an unexpected socket drop (Wi-Fi blip etc.): on a
  // connection loss that we did not initiate, _connect is retried with a
  // backoff of 3s, 6s, 12s, 24s, 30s (capped) until _maxReconnectAttempts is
  // reached; then the UI falls back to the manual Reconnect button.
  // RelayClient.disconnect() never emits connectionState=false, so any drop
  // reported by the live client is genuine and safe to retry on.
  Timer? _reconnectTimer;
  int _reconnectAttempts = 0;
  bool _reconnecting = false;
  static const int _maxReconnectAttempts = 5;
  static const int _maxReconnectDelaySecs = 30;

  // Instant reconnect after a network switch: instead of waiting for the OS
  // to notice the dead socket (10-30s), connectivity_plus tells us the moment
  // the networks change. On loss the socket is treated as dead right away;
  // on return the raced reconnect (saved address vs discovery) fires
  // immediately, skipping any pending backoff.
  StreamSubscription<List<ConnectivityResult>>? _connectivitySub;
  // Guards against stacking a connectivity-triggered reconnect on top of one
  // that is still in flight.
  bool _instantReconnecting = false;

  // Hardware volume keys, forwarded by MainActivity while the app is in the
  // foreground ("up"/"down" string messages).
  static const _volumeKeysChannel = MethodChannel('pcocket/volume_keys');
  // Lock tap on the home screen widget, forwarded by WidgetActionReceiver.
  static const _widgetActionsChannel = MethodChannel('pcocket/widget_actions');

  // Media sleep timer: pauses whatever plays on the PC when it expires. One
  // timer at a time; _sleepTick just refreshes the countdown chip once a
  // second, _sleepTimer does the actual firing.
  Timer? _sleepTimer;
  Timer? _sleepTick;
  DateTime? _sleepEnd;

  // Now Playing: the dashboard polls `media.info` every 3s while connected.
  // Between polls the position is interpolated forward from the last sample
  // (while playing), refreshed once a second by _mediaTick. While the seek
  // slider is being dragged the interpolation freezes and the slider shows
  // _dragValue instead, so poll updates never fight the user's thumb.
  Timer? _mediaPollTimer;
  Timer? _mediaTick;
  String _mediaTitle = '';
  String _mediaArtist = '';
  double _mediaPosition = 0;
  double _mediaDuration = 0;
  bool _mediaPlaying = false;
  DateTime? _mediaInfoAt;
  bool _seekDragging = false;
  double? _dragValue;

  // Temporary diagnostics for the network-switch bug (visible in logcat).
  void _dbg(String msg) => debugPrint('[Sudo] $msg');

  @override
  void initState() {
    super.initState();
    _loadDevices();
    _connectivitySub =
        Connectivity().onConnectivityChanged.listen(_onConnectivityChanged);
    _volumeKeysChannel.setMethodCallHandler((call) async {
      if (call.method == 'volumeKey' && call.arguments is String) {
        _onVolumeKey(call.arguments as String);
      }
    });
    _widgetActionsChannel.setMethodCallHandler((call) async {
      if (call.method == 'lock') _lockFromWidget();
    });
  }

  @override
  void dispose() {
    _connectivitySub?.cancel();
    _volumeKeysChannel.setMethodCallHandler(null);
    _widgetActionsChannel.setMethodCallHandler(null);
    _cancelSleepTimer(update: false);
    _cancelReconnect();
    _tearDownClient();
    super.dispose();
  }

  /// Network-switch fast path. The stream emits the full list of active
  /// connectivity types; anything usable (wifi/mobile/ethernet/vpn) counts
  /// as online.
  void _onConnectivityChanged(List<ConnectivityResult> results) {
    final online = results.any((r) =>
        r == ConnectivityResult.wifi ||
        r == ConnectivityResult.mobile ||
        r == ConnectivityResult.ethernet ||
        r == ConnectivityResult.vpn);
    if (!mounted) return;
    if (!online) {
      // Connectivity lost: force the same state a socket close would have
      // produced instead of waiting for OS-level TCP timeouts.
      if (_socketUp) {
        _dbg('connectivity lost — treating socket as dead');
        setState(() => _socketUp = false);
        _scheduleReconnect();
      }
      return;
    }
    // Connectivity is back and we have a PC to reach: cancel any pending
    // backoff and run the raced reconnect (direct vs discovery) right now.
    if (_selected != null &&
        !_socketUp &&
        !_connecting &&
        !_instantReconnecting) {
      _dbg('connectivity back — reconnecting immediately');
      _cancelReconnect();
      _instantReconnecting = true;
      unawaited(_connect(_selected!, auto: true)
          .whenComplete(() => _instantReconnecting = false));
    }
  }

  /// Hardware volume key while the app is in the foreground: adjust the PC's
  /// volume, fire-and-forget (the phone's own volume stays untouched — the
  /// key event is consumed on the native side).
  void _onVolumeKey(String direction) {
    final client = _client;
    if (client == null || !_socketUp) return;
    unawaited(client
        .request('media', {
          'action':
              direction == 'up' ? 'media_volume_up' : 'media_volume_down',
        })
        .then((_) => true, onError: (_) => false));
  }

  /// Lock tap on the home screen widget. The native receiver only forwards
  /// here while the app is alive; otherwise it just launches the app.
  void _lockFromWidget() {
    final client = _client;
    if (client == null || !_socketUp) return;
    unawaited(client
        .request('power', {'action': 'lock'})
        .then((_) => true, onError: (_) => false));
  }

  /// Pushes the current PC name + connection state to the home screen widget.
  void _syncWidget({required bool connected}) {
    final device = _selected ?? (_devices.isNotEmpty ? _devices.last : null);
    final name = device == null
        ? ''
        : (device.name.isEmpty ? device.code : device.name);
    unawaited(PcWidget.sync(pcName: name, connected: connected));
  }

  Future<void> _loadDevices() async {
    final devices = await _store.load();
    if (!mounted) return;
    setState(() {
      _devices = devices;
      _loadingDevices = false;
    });
    if (_autoConnectPending) {
      _autoConnectPending = false;
      if (devices.isNotEmpty && _selected == null) {
        unawaited(_connect(devices.last));
      }
    }
    // The saved device may have changed — refresh the widget's name/dot.
    if (_selected == null) _syncWidget(connected: false);
  }

  void _tearDownClient() {
    _stopMediaPoll();
    _eventSub?.cancel();
    _connSub?.cancel();
    _eventSub = null;
    _connSub = null;
    final old = _client;
    _client = null;
    if (old != null) {
      ClientRegistry.instance.clear(old);
      unawaited(old.disconnect());
    }
  }

  /// Cancels a pending auto-reconnect and resets the backoff. Called on any
  /// user-initiated connect/disconnect and on dispose.
  void _cancelReconnect() {
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _reconnectAttempts = 0;
    _reconnecting = false;
  }

  /// Schedules the next automatic reconnect attempt, or gives up and returns
  /// to the offline state (manual Reconnect button) once the attempts are
  /// exhausted.
  void _scheduleReconnect() {
    if (!mounted || _reconnectTimer != null) return;
    final device = _selected;
    if (device == null) return;
    if (_reconnectAttempts >= _maxReconnectAttempts) {
      if (_reconnecting) setState(() => _reconnecting = false);
      return;
    }
    var secs = 3 << _reconnectAttempts; // 3, 6, 12, 24, 48…
    if (secs > _maxReconnectDelaySecs) secs = _maxReconnectDelaySecs;
    _reconnectAttempts++;
    _dbg('reconnect attempt $_reconnectAttempts in ${secs}s '
        '(device=${device.relayUrl})');
    setState(() => _reconnecting = true);
    _reconnectTimer = Timer(Duration(seconds: secs), () {
      _reconnectTimer = null;
      if (!mounted || _selected == null) return;
      unawaited(_connect(device, auto: true));
    });
  }

  /// Connects to [device]. With [auto] the attempt is one of the scheduled
  /// reconnects: the backoff state is kept, the dashboard stays visible (the
  /// header shows "Reconnecting…"), and a failure schedules the next attempt
  /// instead of showing an error.
  Future<void> _connect(SavedDevice device, {bool auto = false}) async {
    _dbg('_connect(auto=$auto) to ${device.relayUrl} name=${device.name}');
    if (!auto) _cancelReconnect();
    _tearDownClient();
    setState(() {
      _selected = device;
      if (!auto) _connecting = true;
      _socketUp = false;
      _agentOnline = true;
      _approvalPending = false;
      _sysInfo = null;
      _viaText = null;
    });
    RelayClient? winner;
    var current = device;
    PairResult? result;
    // Last connect/pair error; surfaced when it carries the real reason
    // (e.g. the user tapped Deny on the PC → "denied on this PC").
    Object? failure;
    // Set once the agent asks for approval on the PC: the dialog there
    // auto-denies after 30s, so the waits below get a longer bound (~60s).
    var approvalSeen = false;

    void noteApprovalPending() {
      approvalSeen = true;
      if (mounted) setState(() => _approvalPending = true);
    }

    // The agent's denial arrives as {type:"error", error:"denied on this PC"}.
    bool isDenial(Object? e) =>
        e is RelayException && e.message.contains('denied');

    Future<PairResult?> attempt(RelayClient c, SavedDevice d) async {
      // Hard OUTER timeout: on a blackholed address (phone switched
      // networks) inner socket timeouts have proven unreliable — a
      // connect attempt hung forever and froze the whole retry loop.
      // When the PC shows an approval dialog the bound is extended: the
      // final paired/error answer can take the dialog's full 30s.
      Timer? outerTimer;
      final outer = Completer<void>();
      void armOuter(Duration duration) {
        outerTimer?.cancel();
        outerTimer = Timer(duration, () {
          if (!outer.isCompleted) outer.complete();
        });
      }

      armOuter(const Duration(seconds: 6));
      try {
        return await Future.any<PairResult?>([
          connectAndPair(c,
              relayUrl: d.relayUrl,
              code: d.code,
              token: d.token,
              name: await DeviceStore.phoneName(),
              timeout: const Duration(seconds: 4),
              onApprovalPending: () {
                armOuter(const Duration(seconds: 60));
                noteApprovalPending();
              }),
          outer.future.then<PairResult?>(
              (_) => throw TimeoutException('connect attempt timed out')),
        ]);
      } catch (e) {
        _dbg('connect to ${d.relayUrl} failed: $e');
        failure = e;
        unawaited(
            c.disconnect().timeout(const Duration(seconds: 2), onTimeout: () {}));
        return null;
      } finally {
        outerTimer?.cancel();
      }
    }

    if (auto) {
      // Recovery after a network switch: race the saved address against a
      // fresh discovery — first success wins; if both fail we're out in
      // ~6s instead of hanging or grinding sequential timeouts.
      final race = Completer<void>();
      var failures = 0;
      final c1 = RelayClient();
      final c2 = RelayClient();
      void win(RelayClient c, PairResult r) {
        if (race.isCompleted) return;
        winner = c;
        result = r;
        race.complete();
      }

      void lose() {
        if (++failures == 2 && !race.isCompleted) race.complete();
      }

      unawaited(attempt(c1, current).then((r) {
        if (r != null) {
          win(c1, r);
        } else {
          lose();
        }
      }));
      unawaited(() async {
        final rd = await _rediscover(current);
        if (rd == null) {
          lose();
          return;
        }
        current = rd;
        final r = await attempt(c2, rd);
        if (r != null) {
          win(c2, r);
        } else {
          lose();
        }
      }());
      // Wait for a winner (or both attempts failing). While the PC shows
      // an approval dialog the user needs time to react, so the bound
      // stretches from 12s to ~65s (dialog auto-denies after 30s).
      final waitStart = DateTime.now();
      while (!race.isCompleted) {
        final budget = Duration(seconds: approvalSeen ? 65 : 12);
        if (DateTime.now().difference(waitStart) >= budget) break;
        await Future<void>.delayed(const Duration(milliseconds: 200));
      }
      for (final c in [c1, c2]) {
        if (!identical(c, winner)) {
          unawaited(c
              .disconnect()
              .timeout(const Duration(seconds: 2), onTimeout: () {}));
        }
      }
    } else {
      final client = RelayClient();
      result = await attempt(client, current);
      // A denial is final — a rediscovery retry would just pop the dialog
      // on the PC again.
      if (result == null && !isDenial(failure)) {
        // The saved address is stale (e.g. the PC got a new IP after a DHCP
        // renewal): discover PCs on the LAN once and retry the same PC,
        // matched by its name, at the new address.
        final rediscovered = await _rediscover(current);
        if (rediscovered != null) {
          current = rediscovered;
          result = await attempt(client, current);
        }
      }
      if (result != null) {
        winner = client;
      } else {
        unawaited(client
            .disconnect()
            .timeout(const Duration(seconds: 2), onTimeout: () {}));
      }
    }

    if (result == null || winner == null) {
      if (!mounted) return;
      // A denial is final: retrying would keep popping the dialog on the
      // PC. Show the agent's exact reason ("denied on this PC") instead of
      // the generic connect failure, and stop any auto-reconnect loop.
      final denied = isDenial(failure);
      setState(() {
        _connecting = false;
        _socketUp = false;
        _approvalPending = false;
        if (denied) _reconnecting = false;
      });
      if (auto && !denied && _selected != null) {
        // Keep the retry loop going; _scheduleReconnect applies the next
        // backoff step and eventually gives up.
        _scheduleReconnect();
      } else {
        showError(
            context,
            denied
                ? failure!
                : RelayException('Could not connect to '
                    '${current.name.isEmpty ? current.relayUrl : current.name}'));
      }
      return;
    }
    final client = winner!;
    _connSub = client.connectionState.listen((up) {
      if (!mounted) return;
      setState(() => _socketUp = up);
      _syncWidget(connected: up);
      // The Now Playing poll only runs while the socket is up.
      if (up) {
        _startMediaPoll();
      } else {
        _stopMediaPoll();
      }
      // Only the live client can trigger a reconnect: a drop reported while
      // a (re)connect is still in flight (client not yet assigned to
      // _client) is handled by that attempt's own failure path.
      if (!up && identical(client, _client)) _scheduleReconnect();
    });
    _eventSub = client.events.listen(_onEvent);
    _client = client;
    ClientRegistry.instance.set(client);
    if (!mounted) return;
    // Persist the trust credential the agent issued so future connects skip
    // the on-PC approval prompt (older agents send none → keep the token we
    // already had).
    final newToken = result!.paired['token'] as String? ?? '';
    if (newToken.isNotEmpty && newToken != current.token) {
      current = SavedDevice(
          relayUrl: current.relayUrl,
          code: current.code,
          name: current.name,
          token: newToken);
      // save() dedupes by code+relayUrl — same key here, so the existing
      // entry is replaced in place.
      await _store.save(current);
      if (!mounted) return;
    }
    final usedHost = Uri.tryParse(result!.usedUrl)?.host ?? result!.usedUrl;
    setState(() {
      _selected = current;
      _connecting = false;
      _approvalPending = false;
      _reconnecting = false;
      _reconnectAttempts = 0;
      _socketUp = true;
      _viaText = 'via $usedHost';
    });
    _syncWidget(connected: true);
    // Best effort: mirror PC clipboard changes onto the phone.
    unawaited(client
        .request('clipboard.watch', {'enabled': true})
        .then((_) => true, onError: (_) => false));
    unawaited(_refreshSysInfo());
    _startMediaPoll();
  }

  /// Runs LAN discovery once and, when a PC with the same name as [device]
  /// answers, updates the saved entry to its current address. Returns the
  /// updated device, or null when the PC was not found. Never throws —
  /// discovery can fail while the network is still transitioning.
  Future<SavedDevice?> _rediscover(SavedDevice device) async {
    if (device.name.isEmpty) return null;
    List<DiscoveredPc> found;
    try {
      found = await discoverPcs();
    } catch (e) {
      _dbg('discovery error: $e');
      return null;
    }
    if (!mounted) return null;
    _dbg('discovery found: ${found.map((p) => '${p.name}@${p.relayUrl}').toList()}');
    final name = device.name.toLowerCase();
    final matches =
        found.where((pc) => pc.name.toLowerCase() == name).toList();
    if (matches.isEmpty) {
      _dbg('rediscover: no PC named "$name" among ${found.length} found');
      return null;
    }
    final pc = matches.first;
    if (pc.relayUrl == device.relayUrl) return device;
    final updated = SavedDevice(
        relayUrl: pc.relayUrl,
        code: device.code,
        name: device.name,
        token: device.token);
    // save() dedupes by code+relayUrl, so remove the stale entry first or
    // the updated address would be stored alongside it.
    await _store.delete(device);
    await _store.save(updated);
    return updated;
  }

  void _onEvent(RelayEvent event) {
    if (!mounted) return;
    switch (event.name) {
      case 'agent.online':
        setState(() => _agentOnline = true);
        unawaited(_refreshSysInfo());
      case 'agent.offline':
        setState(() => _agentOnline = false);
      case 'clipboard.changed':
        // Never surface PC clipboard popups — clipboard sync stays manual.
        return;
    }
  }

  Future<void> _refreshSysInfo() async {
    final client = _client;
    if (client == null || _sysInfoLoading) return;
    setState(() => _sysInfoLoading = true);
    try {
      final info = await client.request('sys.info');
      if (!mounted) return;
      setState(() => _sysInfo = info);
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _sysInfoLoading = false);
    }
  }

  void _leaveDashboard() {
    _cancelReconnect();
    _tearDownClient();
    setState(() {
      _selected = null;
      _sysInfo = null;
    });
    _syncWidget(connected: false);
    _loadDevices();
  }

  Future<void> _addDevice() async {
    final added = await Navigator.pushNamed(context, '/pair');
    if (!mounted) return;
    await _loadDevices();
    // Jump straight into the dashboard for the PC that was just paired.
    if (added == true && _selected == null && _devices.isNotEmpty) {
      unawaited(_connect(_devices.last));
    }
  }

  Future<void> _deleteDevice(SavedDevice device) async {
    await _store.delete(device);
    if (mounted) _loadDevices();
  }

  Future<void> _runCommand(String cmd, [Map<String, dynamic> params = const {}]) async {
    final client = _client;
    if (client == null) return;
    try {
      await client.request(cmd, params);
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  Future<void> _pasteFromPc() async {
    final client = _client;
    if (client == null) return;
    try {
      final data = await client.request('clipboard.get');
      final text = '${data['text'] ?? ''}';
      await Clipboard.setData(ClipboardData(text: text));
      if (mounted) showMessage(context, 'Copied PC clipboard to phone');
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  Future<void> _sendToPc() async {
    final client = _client;
    if (client == null) return;
    final ctrl = TextEditingController();
    final text = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Send to PC clipboard'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          maxLines: null,
          decoration: const InputDecoration(
            hintText: 'Text to paste on the PC',
            border: OutlineInputBorder(),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, ctrl.text),
            child: const Text('Send'),
          ),
        ],
      ),
    );
    if (text == null || text.isEmpty) return;
    try {
      await client.request('clipboard.set', {'text': text});
      if (mounted) showMessage(context, 'Sent to PC clipboard');
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  Future<void> _power(String action, String label, {bool dangerous = false}) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('$label PC?'),
        content: Text(
          dangerous
              ? 'This will $action the PC and close everything running on it. Unsaved work will be lost.'
              : 'Are you sure you want to $action the PC?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: Text(label),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    await _runCommand('power', {'action': action});
    if (mounted) showMessage(context, '$label command sent');
  }

  /// Preset picker for the media sleep timer (AlertDialog house style).
  Future<void> _showSleepTimerDialog() async {
    final minutes = await showDialog<int>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Sleep timer'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text('Pause media on the PC after…'),
            for (final m in const [15, 30, 45, 60])
              ListTile(
                dense: true,
                title: Text('$m minutes'),
                onTap: () => Navigator.pop(ctx, m),
              ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
        ],
      ),
    );
    if (minutes != null) _startSleepTimer(minutes);
  }

  void _startSleepTimer(int minutes) {
    _cancelSleepTimer(update: false);
    setState(
        () => _sleepEnd = DateTime.now().add(Duration(minutes: minutes)));
    _sleepTimer = Timer(Duration(minutes: minutes), _onSleepTimerExpired);
    // Refreshes the countdown chip; stops itself once the end is reached.
    _sleepTick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      final end = _sleepEnd;
      if (end == null || !end.isAfter(DateTime.now())) return;
      setState(() {});
    });
  }

  void _cancelSleepTimer({bool update = true}) {
    _sleepTimer?.cancel();
    _sleepTick?.cancel();
    _sleepTimer = null;
    _sleepTick = null;
    _sleepEnd = null;
    if (update && mounted) setState(() {});
  }

  /// Expiry: pause whatever is playing on the PC, once, fire-and-forget.
  void _onSleepTimerExpired() {
    _cancelSleepTimer();
    final client = _client;
    if (client != null && _socketUp) {
      unawaited(client
          .request('media', {'action': 'media_play_pause'})
          .then((_) => true, onError: (_) => false));
    }
  }

  /// Starts the 3s `media.info` poll + the 1s interpolation tick. Called
  /// once the dashboard is connected; idempotent.
  void _startMediaPoll() {
    _stopMediaPoll();
    _mediaPollTimer = Timer.periodic(
        const Duration(seconds: 3), (_) => unawaited(_pollMediaInfo()));
    _mediaTick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted || !_mediaPlaying || _seekDragging) return;
      setState(() {}); // advance the interpolated position
    });
    unawaited(_pollMediaInfo());
  }

  /// Stops the poll and drops the last known track, so a reconnect does not
  /// show stale metadata.
  void _stopMediaPoll() {
    _mediaPollTimer?.cancel();
    _mediaTick?.cancel();
    _mediaPollTimer = null;
    _mediaTick = null;
    _mediaTitle = '';
    _mediaArtist = '';
    _mediaPlaying = false;
    _seekDragging = false;
    _dragValue = null;
  }

  Future<void> _pollMediaInfo() async {
    final client = _client;
    if (client == null || !_socketUp) return;
    try {
      final info = await client.request('media.info');
      if (!mounted) return;
      setState(() {
        _mediaTitle = '${info['title'] ?? ''}';
        _mediaArtist = '${info['artist'] ?? ''}';
        _mediaDuration = ((info['duration_s'] as num?) ?? 0).toDouble();
        _mediaPosition = ((info['position_s'] as num?) ?? 0).toDouble();
        _mediaPlaying = info['playing'] == true;
        _mediaInfoAt = DateTime.now();
        if (!_seekDragging) _dragValue = null;
      });
    } catch (_) {
      // Poll failures are transient (socket blip, player gone); the next
      // tick retries — never surface an error popup for these.
    }
  }

  /// Position to show on the seek slider: the drag value while dragging,
  /// otherwise the last polled position advanced by wall-clock time when
  /// playing (clamped to the track duration).
  double get _displayPosition {
    if (_seekDragging && _dragValue != null) return _dragValue!;
    var pos = _mediaPosition;
    final at = _mediaInfoAt;
    if (_mediaPlaying && at != null) {
      pos += DateTime.now().difference(at).inMilliseconds / 1000.0;
    }
    if (_mediaDuration > 0 && pos > _mediaDuration) pos = _mediaDuration;
    if (pos < 0) pos = 0;
    return pos;
  }

  /// Slider release: jump locally (no waiting for the next poll) and send
  /// the seek, fire-and-forget.
  void _seekTo(double seconds) {
    setState(() {
      _seekDragging = false;
      _dragValue = null;
      _mediaPosition = seconds;
      _mediaInfoAt = DateTime.now();
    });
    final client = _client;
    if (client == null || !_socketUp) return;
    unawaited(client
        .request('media.seek', {'seconds': seconds.round()})
        .then((_) => true, onError: (_) => false));
  }

  /// Remaining time as `m:ss` for the countdown chip.
  String get _sleepTimerLabel {
    final end = _sleepEnd;
    if (end == null) return '';
    var secs = end.difference(DateTime.now()).inSeconds;
    if (secs < 0) secs = 0;
    return '${secs ~/ 60}:${(secs % 60).toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    if (_selected == null) return _buildDeviceList();
    return _buildDashboard();
  }

  Widget _buildDeviceList() {
    return Scaffold(
      appBar: AppBar(title: const Text('My PCs')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: _addDevice,
        icon: const Icon(Icons.add),
        label: const Text('Add PC'),
      ),
      body: _loadingDevices
          ? const Center(child: CircularProgressIndicator())
          : _devices.isEmpty
              ? const Center(child: Text('No saved PCs yet'))
              : RefreshIndicator(
                  onRefresh: _loadDevices,
                  child: ListView.builder(
                    itemCount: _devices.length,
                    itemBuilder: (context, i) {
                      final d = _devices[i];
                      return Card(
                        margin: const EdgeInsets.symmetric(
                            horizontal: 16, vertical: 6),
                        child: ListTile(
                          leading: const Icon(Icons.computer, size: 36),
                          title: Text(d.name.isEmpty ? d.code : d.name),
                          subtitle: Text(d.relayUrl),
                          trailing: IconButton(
                            icon: const Icon(Icons.delete_outline),
                            tooltip: 'Remove',
                            onPressed: () => _deleteDevice(d),
                          ),
                          onTap: () => _connect(d),
                        ),
                      );
                    },
                  ),
                ),
    );
  }

  Widget _buildDashboard() {
    final device = _selected!;
    final theme = Theme.of(context);
    final online = _socketUp && _agentOnline;
    final statusText = _approvalPending
        ? 'Waiting for approval on the PC…'
        : _reconnecting
            ? 'Reconnecting…'
            : !_socketUp
                ? 'Disconnected'
                : _agentOnline
                    ? 'Connected'
                    : 'PC is offline';
    final deviceName = device.name.isEmpty ? device.code : device.name;
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: _leaveDashboard,
        ),
        title: Text(deviceName),
      ),
      body: _connecting
          ? Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const CircularProgressIndicator(),
                  const SizedBox(height: 16),
                  Text(_approvalPending
                      ? 'Waiting for approval on the PC…'
                      : 'Connecting to $deviceName…'),
                  if (_approvalPending) ...[
                    const SizedBox(height: 8),
                    Text(
                      'Tap Allow in the dialog shown on your PC.',
                      style: theme.textTheme.bodySmall,
                    ),
                  ],
                ],
              ),
            )
          : ListView(
              padding: const EdgeInsets.all(16),
              children: [
                _buildHeaderCard(device, statusText, online),
                const SizedBox(height: 12),
                _buildSysInfoCard(theme),
                const SizedBox(height: 24),
                Text('Control', style: theme.textTheme.titleMedium),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(
                      child: _ControlCard(
                        icon: Icons.monitor,
                        label: 'Screen',
                        onTap: () => _open(ScreenPage(client: _client!)),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _ControlCard(
                        icon: Icons.folder_open,
                        label: 'Files',
                        onTap: () => _open(FilesPage(client: _client!)),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: _ControlCard(
                        icon: Icons.videocam,
                        label: 'Camera',
                        onTap: () => _open(CameraPage(client: _client!)),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 24),
                Text('Quick actions', style: theme.textTheme.titleMedium),
                const SizedBox(height: 8),
                _buildMediaCard(theme),
                const SizedBox(height: 8),
                _buildClipboardCard(theme),
                const SizedBox(height: 8),
                _buildPowerCard(theme),
              ],
            ),
    );
  }

  Widget _buildHeaderCard(SavedDevice device, String statusText, bool online) {
    final theme = Theme.of(context);
    final platform = '${_sysInfo?['platform'] ?? ''}';
    return Container(
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          colors: [Color(0xFF0D9488), Color(0xFF22C55E)],
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
        ),
        borderRadius: BorderRadius.circular(16),
      ),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.computer, color: Colors.white, size: 32),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  device.name.isEmpty ? device.code : device.name,
                  style: theme.textTheme.titleLarge?.copyWith(
                    color: Colors.white,
                    fontWeight: FontWeight.bold,
                  ),
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (!online)
                _reconnecting
                    ? const SizedBox(
                        width: 24,
                        height: 24,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: Colors.white70,
                        ),
                      )
                    : Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          FilledButton.icon(
                            onPressed: () => _connect(device),
                            icon: const Icon(Icons.refresh, size: 18),
                            label: const Text('Reconnect'),
                            style: FilledButton.styleFrom(
                              backgroundColor: Colors.white,
                              foregroundColor: const Color(0xFF0D9488),
                            ),
                          ),
                          const SizedBox(width: 8),
                          // The way out when the saved address is hopeless
                          // (e.g. new network): the plain PC search, no
                          // deleting the saved device needed.
                          OutlinedButton.icon(
                            onPressed: _addDevice,
                            icon: const Icon(Icons.wifi_find, size: 18),
                            label: const Text('Find PC'),
                            style: OutlinedButton.styleFrom(
                              foregroundColor: Colors.white,
                              side: const BorderSide(color: Colors.white70),
                            ),
                          ),
                        ],
                      ),
            ],
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Icon(
                Icons.circle,
                size: 10,
                color: online
                    ? Colors.greenAccent
                    : _reconnecting
                        ? Colors.amberAccent
                        : Colors.white38,
              ),
              const SizedBox(width: 8),
              Text(statusText,
                  style: const TextStyle(color: Colors.white70)),
              if (platform.isNotEmpty) ...[
                const Text('  ·  ',
                    style: TextStyle(color: Colors.white38)),
                Flexible(
                  child: Text(
                    platform,
                    style: const TextStyle(color: Colors.white70),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ],
          ),
          if (_viaText != null) ...[
            const SizedBox(height: 6),
            Text(
              _viaText!,
              style: const TextStyle(color: Colors.white54, fontSize: 12),
            ),
          ],
        ],
      ),
    );
  }

  /// Compact icon button for the quick-action rows: 40x40 effective size
  /// (22px icon + 4px padding under compact density), so a full row of
  /// them fits on one line on a ~360px-wide phone.
  Widget _quickAction({
    required IconData icon,
    required String tooltip,
    required VoidCallback onPressed,
    Color? color,
  }) {
    return IconButton(
      tooltip: tooltip,
      iconSize: 22,
      padding: const EdgeInsets.all(4),
      visualDensity: VisualDensity.compact,
      icon: Icon(icon, color: color),
      onPressed: onPressed,
    );
  }

  /// Spreads quick-action buttons across the card on a single line. The
  /// per-button FittedBox is a last-resort guard that scales buttons down
  /// instead of overflowing on very narrow screens.
  Widget _oneLineRow(List<Widget> buttons) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        for (final button in buttons)
          Flexible(child: FittedBox(fit: BoxFit.scaleDown, child: button)),
      ],
    );
  }

  Widget _buildMediaCard(ThemeData theme) {
    final hasTrack = _mediaTitle.isNotEmpty;
    // Seeking only makes sense when the track reports a real duration.
    final seekable = hasTrack && _mediaDuration > 0;
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Text('Media', style: theme.textTheme.titleSmall),
                const Spacer(),
                if (_sleepEnd != null)
                  Padding(
                    padding: const EdgeInsets.only(right: 4),
                    child: ActionChip(
                      avatar: const Icon(Icons.timer, size: 16),
                      label: Text(_sleepTimerLabel),
                      tooltip: 'Cancel sleep timer',
                      visualDensity: VisualDensity.compact,
                      onPressed: _cancelSleepTimer,
                    ),
                  ),
                _quickAction(
                  tooltip: 'Sleep timer',
                  icon: Icons.timer_outlined,
                  onPressed: _showSleepTimerDialog,
                ),
              ],
            ),
            if (hasTrack) ...[
              const SizedBox(height: 4),
              Text(
                _mediaTitle,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodyLarge,
              ),
              if (_mediaArtist.isNotEmpty)
                Text(
                  _mediaArtist,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.hintColor),
                ),
              // Seek bar: shows the polled position interpolated forward
              // between polls; dragging freezes interpolation and the
              // release sends media.seek.
              Slider(
                value: seekable ? _displayPosition : 0,
                max: seekable ? _mediaDuration : 1,
                onChangeStart: seekable
                    ? (_) => setState(() => _seekDragging = true)
                    : null,
                onChanged: seekable
                    ? (v) => setState(() => _dragValue = v)
                    : null,
                onChangeEnd: seekable ? _seekTo : null,
              ),
              _oneLineRow([
                _quickAction(
                  tooltip: 'Previous',
                  icon: Icons.skip_previous,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_previous'}),
                ),
                _quickAction(
                  tooltip: 'Play / pause',
                  icon: _mediaPlaying ? Icons.pause : Icons.play_arrow,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_play_pause'}),
                ),
                _quickAction(
                  tooltip: 'Next',
                  icon: Icons.skip_next,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_next'}),
                ),
                _quickAction(
                  tooltip: 'Volume down',
                  icon: Icons.volume_down,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_volume_down'}),
                ),
                _quickAction(
                  tooltip: 'Volume up',
                  icon: Icons.volume_up,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_volume_up'}),
                ),
                _quickAction(
                  tooltip: 'Mute',
                  icon: Icons.volume_off,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_volume_mute'}),
                ),
              ]),
            ] else ...[
              const SizedBox(height: 8),
              Text(
                'Nothing playing',
                style: theme.textTheme.bodyMedium
                    ?.copyWith(color: theme.hintColor),
              ),
              _oneLineRow([
                _quickAction(
                  tooltip: 'Volume down',
                  icon: Icons.volume_down,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_volume_down'}),
                ),
                _quickAction(
                  tooltip: 'Volume up',
                  icon: Icons.volume_up,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_volume_up'}),
                ),
                _quickAction(
                  tooltip: 'Mute',
                  icon: Icons.volume_off,
                  onPressed: () =>
                      _runCommand('media', {'action': 'media_volume_mute'}),
                ),
              ]),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildClipboardCard(ThemeData theme) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Clipboard', style: theme.textTheme.titleSmall),
            const SizedBox(height: 8),
            _oneLineRow([
              FilledButton.tonalIcon(
                onPressed: _pasteFromPc,
                icon: const Icon(Icons.content_paste),
                label: const Text('Paste from PC'),
              ),
              FilledButton.tonalIcon(
                onPressed: _sendToPc,
                icon: const Icon(Icons.send_to_mobile),
                label: const Text('Send to PC'),
              ),
            ]),
          ],
        ),
      ),
    );
  }

  Widget _buildPowerCard(ThemeData theme) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Power', style: theme.textTheme.titleSmall),
            const SizedBox(height: 8),
            _oneLineRow([
              _quickAction(
                tooltip: 'Lock',
                icon: Icons.lock_outline,
                onPressed: () => _power('lock', 'Lock'),
              ),
              _quickAction(
                tooltip: 'Sleep',
                icon: Icons.bedtime_outlined,
                onPressed: () => _power('sleep', 'Sleep'),
              ),
              _quickAction(
                tooltip: 'Restart',
                icon: Icons.restart_alt,
                onPressed: () =>
                    _power('restart', 'Restart', dangerous: true),
              ),
              _quickAction(
                tooltip: 'Shut down',
                icon: Icons.power_settings_new,
                onPressed: () =>
                    _power('shutdown', 'Shut down', dangerous: true),
              ),
            ]),
          ],
        ),
      ),
    );
  }

  Widget _buildSysInfoCard(ThemeData theme) {
    final info = _sysInfo;
    return Card(
      // The whole card doubles as a refresh button (the icon stays too).
      child: InkWell(
        onTap: _refreshSysInfo,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Text('System', style: theme.textTheme.titleMedium),
                  const Spacer(),
                  if (_sysInfoLoading)
                    const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  else
                    IconButton(
                      tooltip: 'Refresh',
                      icon: const Icon(Icons.refresh),
                      onPressed: _refreshSysInfo,
                    ),
                ],
              ),
              if (info == null)
                const Text('No data yet')
              else ...[
                _InfoRow('Name', '${info['name'] ?? '?'}'),
                _InfoRow('Platform', '${info['platform'] ?? '?'}'),
                _InfoRow(
                  'CPU',
                  '${(info['cpu_percent'] as num?)?.toStringAsFixed(0) ?? '?'}%',
                  strong: true,
                ),
                _InfoRow(
                  'RAM',
                  '${formatBytes((info['mem_used'] as num?) ?? 0)} / '
                      '${formatBytes((info['mem_total'] as num?) ?? 0)}',
                  strong: true,
                ),
                _InfoRow('Uptime',
                    formatUptime((info['uptime'] as num?) ?? 0)),
              ],
            ],
          ),
        ),
      ),
    );
  }

  void _open(Widget page) {
    if (_client == null || !_socketUp) {
      showMessage(context, 'Not connected');
      return;
    }
    Navigator.push(context, MaterialPageRoute(builder: (_) => page));
  }
}

class _ControlCard extends StatelessWidget {
  const _ControlCard({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 24),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(icon, size: 40),
              const SizedBox(height: 8),
              Text(label, style: Theme.of(context).textTheme.titleSmall),
            ],
          ),
        ),
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  const _InfoRow(this.label, this.value, {this.strong = false});

  final String label;
  final String value;

  /// Renders the value with slightly stronger typography (used for the
  /// live CPU/RAM numbers).
  final bool strong;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          SizedBox(
            width: 80,
            child: Text(label,
                style: TextStyle(color: Theme.of(context).hintColor)),
          ),
          Expanded(
            child: Text(
              value,
              style: strong
                  ? const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)
                  : null,
            ),
          ),
        ],
      ),
    );
  }
}
