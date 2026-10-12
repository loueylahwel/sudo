import 'dart:async';
import 'dart:collection';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../client_registry.dart';
import '../relay_client.dart';
import '../util.dart';

/// Switches the PC to [window] (a `wm.list` entry): restores it if
/// minimized and brings it to the foreground. The default pick action of
/// [showWindowSwitcher]; the Screen page's [AppDock] uses it too.
///
/// The target rides in `window`, never `id`: `id` is the request
/// envelope's own key, and a window id sent there would overwrite it so the
/// reply could never be matched to this request.
Future<void> activateWindow(RelayClient client, Map<String, dynamic> window) =>
    client.request('wm.activate', {'window': window['id']});

/// Opens the app switcher: a searchable 2-column grid of the PC's open
/// windows with live thumbnails (Alt-Tab, but on the phone) and, when
/// [allowLaunch] is true, a "Launch" tab with the Start menu's apps.
///
/// Tapping a window card calls [onPick] with that `wm.list` entry (`id`,
/// `title`, `process`, `exe`, `monitor`, `rect`, ... — see
/// docs/protocol.md) while the card shows a spinner. The sheet closes
/// itself once [onPick] completes; if it throws, the sheet stays open and
/// shows the error. The default [onPick] switches to the window
/// ([activateWindow]).
///
/// Features that act on "a window of the user's choosing" reuse the sheet
/// as their picker by passing their own [onPick] and [title] — e.g. moving
/// a window to the phone screen:
///
/// ```dart
/// await showWindowSwitcher(context, client,
///     title: 'Move to phone',
///     allowLaunch: false, // launching makes no sense for a picker
///     onPick: (w) => moveWindowToPhone(client, w));
/// ```
///
/// Long-press on a card (minimize / maximize / close) stays available in
/// every mode. Completes when the sheet is dismissed.
Future<void> showWindowSwitcher(
  BuildContext context,
  RelayClient client, {
  String title = 'Switch app',
  Future<void> Function(Map<String, dynamic> window)? onPick,
  bool allowLaunch = true,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    showDragHandle: true,
    builder: (ctx) => DraggableScrollableSheet(
      expand: false,
      // Opens at its smallest size and never closes from inside the list:
      // at the top of the list a downward drag goes to the sheet first
      // (shrinking it, then closing it at minChildSize), so with any room
      // to shrink, pull-to-refresh could never be reached. This way a pull
      // refreshes, a drag up still expands the sheet, and the drag handle,
      // the scrim or Back close it.
      initialChildSize: 0.75,
      minChildSize: 0.75,
      maxChildSize: 0.95,
      shouldCloseOnMinExtent: false,
      builder: (ctx, scrollCtrl) => WindowSwitcherSheet(
        client: client,
        scrollController: scrollCtrl,
        title: title,
        // The live client at pick time: an auto-reconnect may have replaced
        // the one the sheet was opened with.
        onPick: onPick ??
            (w) => activateWindow(ClientRegistry.instance.client ?? client, w),
        allowLaunch: allowLaunch,
      ),
    ),
  );
}

/// App icons from `wm.list`, decoded once and shared by the switcher and
/// the dock. Keyed by app (exe path, else process name) like the agent's
/// own cache, so a periodic refresh can ask for `icons: false` and only
/// re-request icons when an app it has never seen shows up. Reusing the
/// SAME Uint8List also keeps Image.memory from re-decoding the PNG on every
/// rebuild (MemoryImage compares its bytes by identity).
class _AppIcons {
  static final Map<String, Uint8List?> _cache = {};

  static String keyOf(Map<String, dynamic> w) {
    final exe = '${w['exe'] ?? ''}';
    if (exe.isNotEmpty) return exe.toLowerCase();
    final process = '${w['process'] ?? ''}';
    return process.isNotEmpty ? 'process:$process' : 'window:${w['id']}';
  }

  static bool knows(Map<String, dynamic> w) => _cache.containsKey(keyOf(w));

  static Uint8List? of(Map<String, dynamic> w) => _cache[keyOf(w)];

  /// Stores the icons of an `icons: true` listing. Windows that came back
  /// without one (older agents, icon-less apps) are remembered as icon-less
  /// so they don't trigger an icon re-request on every refresh.
  static void absorb(List<Map<String, dynamic>> windows) {
    for (final w in windows) {
      final key = keyOf(w);
      Uint8List? bytes;
      final icon = w['icon'];
      if (icon is String && icon.isNotEmpty) {
        try {
          bytes = base64Decode(icon);
        } catch (_) {
          // Corrupt icon; fall back to the generic window glyph.
        }
      }
      if (bytes != null || !_cache.containsKey(key)) _cache[key] = bytes;
    }
  }
}

List<Map<String, dynamic>> _windowsOf(Map<String, dynamic> data) =>
    (data['windows'] as List? ?? const [])
        .whereType<Map>()
        .map((e) => e.cast<String, dynamic>())
        .toList();

/// `wm.list` without re-downloading icons the app already has: a cheap
/// `icons: false` listing first, and an `icons: true` one only when an app
/// not seen before is open (each icon is a few KB of base64).
Future<List<Map<String, dynamic>>> _fetchWindows(RelayClient client) async {
  var windows = _windowsOf(await client.request('wm.list', {'icons': false}));
  if (windows.any((w) => !_AppIcons.knows(w))) {
    windows = _windowsOf(await client.request('wm.list', {'icons': true}));
    _AppIcons.absorb(windows);
  }
  return windows;
}

/// Runs at most [max] requests at once, the rest queued in call order.
/// Cards ask for their thumbnail/icon as they scroll into view; firing a
/// whole grid's worth at once would only queue up on the PC anyway, and
/// in call order the top of the list arrives first.
class _Limiter {
  _Limiter(this.max);

  final int max;
  int _running = 0;
  final Queue<Completer<void>> _waiting = Queue();

  Future<T> run<T>(Future<T> Function() job) async {
    while (_running >= max) {
      final turn = Completer<void>();
      _waiting.add(turn);
      await turn.future;
    }
    _running++;
    try {
      return await job();
    } finally {
      _running--;
      if (_waiting.isNotEmpty) _waiting.removeFirst().complete();
    }
  }
}

/// A `wm.thumb` result. [jpeg] is null for minimized/hung windows (the
/// card shows the big app icon instead) and when the agent can't capture.
class _Thumb {
  const _Thumb(this.jpeg);

  final Uint8List? jpeg;
}

enum _SwitcherTab { windows, launch }

/// The switcher sheet behind [showWindowSwitcher]. Prefer that function;
/// this widget expects to live inside a [DraggableScrollableSheet] whose
/// [scrollController] it attaches to the visible list.
class WindowSwitcherSheet extends StatefulWidget {
  const WindowSwitcherSheet({
    super.key,
    required this.client,
    required this.scrollController,
    required this.onPick,
    this.title = 'Switch app',
    this.allowLaunch = true,
  });

  final RelayClient client;
  final ScrollController scrollController;
  final String title;
  final Future<void> Function(Map<String, dynamic> window) onPick;
  final bool allowLaunch;

  @override
  State<WindowSwitcherSheet> createState() => _WindowSwitcherSheetState();
}

class _WindowSwitcherSheetState extends State<WindowSwitcherSheet> {
  // The Start-menu app list and its icons outlive the sheet: reopening it
  // shows the last list instantly while a fresh one loads (the agent
  // caches Get-StartApps for 10 minutes, so that reply is quick too).
  static List<Map<String, dynamic>>? _cachedApps;
  static final Map<String, Uint8List?> _appIcons = {};

  // Requests go out on this client: the one the sheet was opened with, then
  // whatever HomePage's auto-reconnect publishes (as the Screen page does),
  // so a Wi-Fi blip while the sheet is open doesn't strand it on a dead
  // socket.
  late RelayClient _client = widget.client;
  StreamSubscription<RelayClient>? _registrySub;

  _SwitcherTab _tab = _SwitcherTab.windows;
  final _searchCtrl = TextEditingController();
  String _query = '';

  List<Map<String, dynamic>>? _windows;
  String? _error;
  // The window being picked: its card shows a spinner and every other card
  // is disabled until onPick resolves.
  Object? _busyId;

  // Thumbnails by window id. A refresh re-requests them but keeps showing
  // the old ones until the new ones land, so cards never flash back to the
  // icon placeholder.
  final Map<Object, _Thumb> _thumbs = {};
  final Set<Object> _thumbsRequested = {};
  final _thumbLimiter = _Limiter(3);

  List<Map<String, dynamic>>? _apps = _cachedApps;
  String? _appsError;
  String? _launchingId;
  final Set<String> _appIconsRequested = {};
  final _iconLimiter = _Limiter(4);

  // Inline notice under the search field. Snackbars would render on the
  // Scaffold BEHIND this modal sheet, where nobody can see them.
  String? _notice;
  Timer? _noticeTimer;

  @override
  void initState() {
    super.initState();
    _registrySub = ClientRegistry.instance.onChange.listen((client) {
      if (identical(client, _client)) return;
      _client = client;
      unawaited(_loadWindows());
      if (widget.allowLaunch && _apps == null) unawaited(_loadApps());
    });
    unawaited(_loadWindows());
    // Prefetch so the Launch tab is ready the moment it's opened.
    if (widget.allowLaunch) unawaited(_loadApps());
  }

  @override
  void dispose() {
    _registrySub?.cancel();
    _noticeTimer?.cancel();
    _searchCtrl.dispose();
    super.dispose();
  }

  void _flash(Object message) {
    _noticeTimer?.cancel();
    setState(() => _notice = '$message');
    _noticeTimer = Timer(const Duration(seconds: 4), () {
      if (mounted) setState(() => _notice = null);
    });
  }

  Future<void> _loadWindows() async {
    try {
      final list = await _fetchWindows(_client);
      if (!mounted) return;
      setState(() {
        _windows = list;
        _error = null;
        // Every card fetches a fresh thumbnail on its next build.
        _thumbsRequested.clear();
        _thumbs.removeWhere((id, _) => !list.any((w) => w['id'] == id));
      });
    } catch (e) {
      if (!mounted) return;
      // A failed pull-to-refresh keeps the grid on screen: say why it
      // didn't update instead of failing silently.
      if (_windows != null) {
        _flash(e);
      } else {
        setState(() => _error = '$e');
      }
    }
  }

  Future<void> _loadApps({bool refresh = false}) async {
    try {
      final data = await _client
          .request('apps.list', {if (refresh) 'refresh': true});
      final list = (data['apps'] as List? ?? const [])
          .whereType<Map>()
          .map((e) => e.cast<String, dynamic>())
          .toList();
      for (final app in list) {
        final icon = app['icon'];
        if (icon is String && icon.isNotEmpty) {
          try {
            _appIcons['${app['id']}'] = base64Decode(icon);
          } catch (_) {
            // Corrupt inline icon; apps.icon can still fetch it.
          }
        }
      }
      _cachedApps = list;
      if (!mounted) return;
      setState(() {
        _apps = list;
        _appsError = null;
      });
    } catch (e) {
      if (!mounted) return;
      if (_apps != null) {
        _flash(e);
      } else {
        setState(() => _appsError = '$e');
      }
    }
  }

  /// Runs [request] through [limiter] — unless the sheet has closed by the
  /// time its turn comes (null then). The agent handles this phone's
  /// commands strictly in order, so a backlog of thumbnails/icons nobody
  /// will see would delay the touchpad input sent after the sheet closes.
  Future<Map<String, dynamic>?> _whileOpen(_Limiter limiter,
          Future<Map<String, dynamic>> Function() request) =>
      limiter.run(() async => mounted ? await request() : null);

  /// The thumbnail for [w], requesting it (once per refresh) the first
  /// time its card is built — GridView builds only the cards near the
  /// viewport, so this is the lazy loading.
  _Thumb? _thumbOf(Map<String, dynamic> w) {
    final id = w['id'];
    if (id == null) return null;
    if (_thumbsRequested.add(id)) unawaited(_loadThumb(id));
    return _thumbs[id];
  }

  Future<void> _loadThumb(Object id) async {
    _Thumb thumb;
    try {
      final data = await _whileOpen(_thumbLimiter,
          () => _client.request('wm.thumb', {'window': id, 'max_width': 400}));
      if (data == null) return;
      final jpeg = data['jpeg'];
      thumb = _Thumb(jpeg is String ? base64Decode(jpeg) : null);
    } catch (_) {
      // Older agent without wm.thumb, or the window just closed: the card
      // keeps its icon placeholder.
      thumb = const _Thumb(null);
    }
    if (!mounted) return;
    setState(() => _thumbs[id] = thumb);
  }

  Uint8List? _appIconOf(String id) {
    if (!_appIcons.containsKey(id) && _appIconsRequested.add(id)) {
      unawaited(_loadAppIcon(id));
    }
    return _appIcons[id];
  }

  Future<void> _loadAppIcon(String id) async {
    Map<String, dynamic>? data;
    try {
      data = await _whileOpen(
          _iconLimiter, () => _client.request('apps.icon', {'app': id}));
    } catch (_) {
      // A timeout or reconnect (or an older agent): the row keeps the
      // generic glyph, and nothing is cached so the next open asks again.
      return;
    }
    if (data == null) return;
    Uint8List? bytes;
    final icon = data['icon'];
    if (icon is String && icon.isNotEmpty) {
      try {
        bytes = base64Decode(icon);
      } catch (_) {
        // Corrupt icon; keep the generic glyph.
      }
    }
    // Cached even when null: the agent answered, the app has no icon.
    _appIcons[id] = bytes;
    if (mounted) setState(() {});
  }

  Future<void> _pick(Map<String, dynamic> w) async {
    final id = w['id'];
    if (id == null || _busyId != null) return;
    setState(() => _busyId = id);
    try {
      await widget.onPick(w);
      if (mounted) Navigator.pop(context);
    } catch (e) {
      if (!mounted) return;
      setState(() => _busyId = null);
      _flash(e);
    }
  }

  Future<void> _launch(Map<String, dynamic> app) async {
    final id = '${app['id']}';
    if (_launchingId != null) return;
    setState(() => _launchingId = id);
    try {
      await _client.request('apps.launch', {'app': id});
      if (!mounted) return;
      // Shown on the Screen page once this sheet is gone.
      showMessage(context, 'Starting ${app['name'] ?? 'app'}…');
      Navigator.pop(context);
    } catch (e) {
      if (!mounted) return;
      setState(() => _launchingId = null);
      _flash(e);
    }
  }

  /// Long-press menu of a window card: minimize / maximize / restore /
  /// close, anchored where the finger is.
  Future<void> _windowMenu(Map<String, dynamic> w, Offset globalPos) async {
    unawaited(HapticFeedback.selectionClick());
    final minimized = w['minimized'] == true;
    final maximized = w['maximized'] == true;
    final overlay = Overlay.of(context).context.findRenderObject() as RenderBox;
    PopupMenuItem<String> item(String value, IconData icon, String label,
            {Color? color}) =>
        PopupMenuItem<String>(
          value: value,
          child: Row(
            children: [
              Icon(icon, size: 20, color: color),
              const SizedBox(width: 12),
              Text(label, style: TextStyle(color: color)),
            ],
          ),
        );
    final action = await showMenu<String>(
      context: context,
      position: RelativeRect.fromRect(
          globalPos & const Size(1, 1), Offset.zero & overlay.size),
      items: [
        if (minimized || maximized)
          item('restore', Icons.filter_none, 'Restore'),
        if (!minimized) item('minimize', Icons.minimize, 'Minimize'),
        if (!maximized) item('maximize', Icons.crop_square, 'Maximize'),
        item('close', Icons.close, 'Close', color: Colors.redAccent),
      ],
    );
    if (action == null || !mounted) return;
    if (action == 'close') {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: Text('Close ${w['title'] ?? 'this window'}?'),
          content: const Text(
              'The app may ask on the PC whether to save changes first.'),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Close'),
            ),
          ],
        ),
      );
      if (confirmed != true || !mounted) return;
    }
    try {
      await _client.request('wm.$action', {'window': w['id']});
      // ShowWindowAsync and WM_CLOSE take effect on the PC a moment after
      // the reply: re-list once they have.
      await Future<void>.delayed(const Duration(milliseconds: 350));
      if (mounted) await _loadWindows();
    } catch (e) {
      if (mounted) _flash(e);
    }
  }

  bool _matches(String text) => text.toLowerCase().contains(_query);

  /// Bottom list padding: room for the navigation bar (edge-to-edge) and,
  /// while searching, the keyboard — the sheet itself doesn't resize, so
  /// without this the last rows could never scroll into view.
  double _listBottomPad(BuildContext context) =>
      24 +
      MediaQuery.paddingOf(context).bottom +
      MediaQuery.viewInsetsOf(context).bottom;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final launch = widget.allowLaunch && _tab == _SwitcherTab.launch;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 12, 8),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  widget.title,
                  style: theme.textTheme.titleMedium,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (widget.allowLaunch)
                SegmentedButton<_SwitcherTab>(
                  showSelectedIcon: false,
                  style: const ButtonStyle(
                    visualDensity: VisualDensity.compact,
                    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                  ),
                  segments: const [
                    ButtonSegment(
                        value: _SwitcherTab.windows, label: Text('Open')),
                    ButtonSegment(
                        value: _SwitcherTab.launch, label: Text('Launch')),
                  ],
                  selected: {_tab},
                  onSelectionChanged: (s) {
                    setState(() => _tab = s.first);
                    // A failed prefetch gets a retry when the tab is opened.
                    if (s.first == _SwitcherTab.launch && _apps == null) {
                      unawaited(_loadApps());
                    }
                  },
                ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
          child: TextField(
            controller: _searchCtrl,
            onChanged: (v) => setState(() => _query = v.trim().toLowerCase()),
            textInputAction: TextInputAction.search,
            decoration: InputDecoration(
              hintText: launch ? 'Search apps' : 'Search open windows',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: _query.isEmpty
                  ? null
                  : IconButton(
                      tooltip: 'Clear',
                      icon: const Icon(Icons.close),
                      onPressed: () {
                        _searchCtrl.clear();
                        setState(() => _query = '');
                      },
                    ),
              isDense: true,
              filled: true,
              fillColor: const Color(0xFF161616),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
                borderSide: BorderSide.none,
              ),
            ),
          ),
        ),
        if (_notice != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: Text(
              _notice!,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: theme.colorScheme.error, fontSize: 13),
            ),
          ),
        Expanded(child: launch ? _buildApps() : _buildWindows()),
      ],
    );
  }

  /// A centered message that still scrolls, so pull-to-refresh and the
  /// sheet's drag keep working in the empty / error / loading states.
  Widget _placeholder(Widget child) {
    return ListView(
      controller: widget.scrollController,
      physics: const AlwaysScrollableScrollPhysics(),
      children: [
        const SizedBox(height: 96),
        Center(child: child),
      ],
    );
  }

  Widget _buildWindows() {
    final wins = _windows;
    Widget content;
    if (wins == null) {
      content = _placeholder(_error != null
          ? Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: Text(_error!, textAlign: TextAlign.center),
            )
          : const CircularProgressIndicator());
    } else {
      final shown = _query.isEmpty
          ? wins
          : wins
              .where((w) =>
                  _matches('${w['title'] ?? ''}') ||
                  _matches('${w['process'] ?? ''}'))
              .toList();
      if (shown.isEmpty) {
        content = _placeholder(Text(
            wins.isEmpty ? 'No open windows' : 'No window matches',
            style: const TextStyle(color: Colors.white54)));
      } else {
        content = LayoutBuilder(
          builder: (context, box) {
            const pad = 12.0;
            const gap = 10.0;
            final tileW = (box.maxWidth - pad * 2 - gap) / 2;
            final ts = MediaQuery.textScalerOf(context);
            // Thumbnail (16:10) + paddings + title row + process line,
            // sized from the user's text scale so big fonts never overflow.
            final extent = tileW * 10 / 16 +
                20 +
                math.max(18.0, ts.scale(13) * 1.4) +
                ts.scale(11) * 1.4;
            return GridView.builder(
              controller: widget.scrollController,
              physics: const AlwaysScrollableScrollPhysics(),
              padding:
                  EdgeInsets.fromLTRB(pad, 4, pad, _listBottomPad(context)),
              gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: 2,
                crossAxisSpacing: gap,
                mainAxisSpacing: gap,
                mainAxisExtent: extent,
              ),
              itemCount: shown.length,
              itemBuilder: (context, i) {
                final w = shown[i];
                return _WindowCard(
                  window: w,
                  icon: _AppIcons.of(w),
                  thumb: _thumbOf(w),
                  busy: _busyId != null && _busyId == w['id'],
                  enabled: _busyId == null,
                  onTap: () => unawaited(_pick(w)),
                  onLongPress: (pos) => unawaited(_windowMenu(w, pos)),
                );
              },
            );
          },
        );
      }
    }
    return RefreshIndicator(onRefresh: _loadWindows, child: content);
  }

  Widget _buildApps() {
    final apps = _apps;
    Widget content;
    if (apps == null) {
      content = _placeholder(_appsError != null
          ? Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: Text(_appsError!, textAlign: TextAlign.center),
            )
          : const Column(
              children: [
                CircularProgressIndicator(),
                SizedBox(height: 12),
                Text('Reading the Start menu…',
                    style: TextStyle(color: Colors.white54)),
              ],
            ));
    } else {
      final shown = _query.isEmpty
          ? apps
          : apps.where((a) => _matches('${a['name'] ?? ''}')).toList();
      if (shown.isEmpty) {
        content = _placeholder(Text(
            apps.isEmpty ? 'No apps found' : 'No app matches',
            style: const TextStyle(color: Colors.white54)));
      } else {
        content = ListView.builder(
          controller: widget.scrollController,
          physics: const AlwaysScrollableScrollPhysics(),
          padding: EdgeInsets.fromLTRB(8, 0, 8, _listBottomPad(context)),
          itemCount: shown.length,
          itemBuilder: (context, i) {
            final app = shown[i];
            final id = '${app['id']}';
            final icon = _appIconOf(id);
            return ListTile(
              enabled: _launchingId == null,
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12)),
              leading: SizedBox(
                width: 32,
                height: 32,
                child: icon != null
                    ? Image.memory(icon,
                        gaplessPlayback: true,
                        filterQuality: FilterQuality.medium)
                    : const Icon(Icons.apps, color: Colors.white38),
              ),
              title: Text('${app['name'] ?? id}',
                  maxLines: 1, overflow: TextOverflow.ellipsis),
              trailing: _launchingId == id
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : null,
              onTap: () => unawaited(_launch(app)),
            );
          },
        );
      }
    }
    return RefreshIndicator(
      onRefresh: () => _loadApps(refresh: true),
      child: content,
    );
  }
}

/// One open window in the switcher grid: live thumbnail (app icon as the
/// placeholder, and for minimized windows), icon + title + process name.
/// The foreground window gets a primary-colored outline and an "Active" tag.
class _WindowCard extends StatefulWidget {
  const _WindowCard({
    required this.window,
    required this.icon,
    required this.thumb,
    required this.busy,
    required this.enabled,
    required this.onTap,
    required this.onLongPress,
  });

  final Map<String, dynamic> window;
  final Uint8List? icon;
  final _Thumb? thumb;
  final bool busy;
  final bool enabled;
  final VoidCallback onTap;
  final void Function(Offset globalPosition) onLongPress;

  @override
  State<_WindowCard> createState() => _WindowCardState();
}

class _WindowCardState extends State<_WindowCard> {
  // InkWell's onLongPress carries no position; the preceding tap-down does,
  // and anchors the popup menu under the finger.
  Offset _downPos = Offset.zero;

  @override
  Widget build(BuildContext context) {
    final w = widget.window;
    final scheme = Theme.of(context).colorScheme;
    final active = w['active'] == true;
    final minimized = w['minimized'] == true;
    final jpeg = widget.thumb?.jpeg;
    final icon = widget.icon;

    Widget preview;
    if (jpeg != null) {
      preview = Image.memory(
        jpeg,
        fit: BoxFit.cover,
        alignment: Alignment.topCenter,
        gaplessPlayback: true,
        filterQuality: FilterQuality.medium,
      );
    } else {
      preview = Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            icon != null
                ? Image.memory(icon,
                    width: 40,
                    height: 40,
                    gaplessPlayback: true,
                    filterQuality: FilterQuality.medium)
                : const Icon(Icons.window_outlined,
                    size: 36, color: Colors.white38),
            if (minimized) ...[
              const SizedBox(height: 4),
              const Text('Minimized',
                  style: TextStyle(color: Colors.white38, fontSize: 11)),
            ],
          ],
        ),
      );
    }

    return Material(
      color: const Color(0xFF0F0F0F),
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: active
            ? BorderSide(color: scheme.primary, width: 2)
            : const BorderSide(color: Color(0xFF1E1E1E)),
      ),
      child: InkWell(
        onTapDown: (d) => _downPos = d.globalPosition,
        onTap: widget.enabled ? widget.onTap : null,
        onLongPress:
            widget.enabled ? () => widget.onLongPress(_downPos) : null,
        child: Padding(
          padding: const EdgeInsets.all(6),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(10),
                  child: Stack(
                    fit: StackFit.expand,
                    children: [
                      const ColoredBox(color: Color(0xFF161616)),
                      preview,
                      if (active)
                        Positioned(
                          left: 6,
                          top: 6,
                          child: DecoratedBox(
                            decoration: BoxDecoration(
                              color: scheme.primary,
                              borderRadius: BorderRadius.circular(6),
                            ),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 6, vertical: 2),
                              child: Text(
                                'Active',
                                style: TextStyle(
                                    color: scheme.onPrimary,
                                    fontSize: 10,
                                    fontWeight: FontWeight.w600),
                              ),
                            ),
                          ),
                        ),
                      if (widget.busy)
                        const ColoredBox(
                          color: Colors.black54,
                          child: Center(
                            child: SizedBox(
                              width: 28,
                              height: 28,
                              child:
                                  CircularProgressIndicator(strokeWidth: 2.5),
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 6),
              Row(
                children: [
                  SizedBox(
                    width: 18,
                    height: 18,
                    child: icon != null
                        ? Image.memory(icon,
                            gaplessPlayback: true,
                            filterQuality: FilterQuality.medium)
                        : const Icon(Icons.window_outlined,
                            size: 16, color: Colors.white54),
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      '${w['title'] ?? '?'}',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          fontSize: 13, fontWeight: FontWeight.w500),
                    ),
                  ),
                ],
              ),
              Padding(
                padding: const EdgeInsets.only(left: 24),
                child: Text(
                  '${w['process'] ?? ''}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 11, color: Colors.white54),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Slim taskbar-style dock for the Screen page: one icon per open PC
/// window, the active one highlighted, tap to switch; a grid button opens
/// the full switcher ([onOpenSwitcher]) and a chevron collapses the dock to
/// a small tab so the whole PC screen stays visible.
///
/// Only the dock's own surface takes touches — the space around it stays
/// part of the touchpad underneath, and a drag that starts on the touchpad
/// keeps belonging to the touchpad even when it crosses the dock.
class AppDock extends StatefulWidget {
  const AppDock({
    super.key,
    required this.client,
    required this.onOpenSwitcher,
  });

  final RelayClient client;
  final VoidCallback onOpenSwitcher;

  @override
  State<AppDock> createState() => AppDockState();
}

class AppDockState extends State<AppDock> {
  static const Duration _refreshEvery = Duration(seconds: 5);
  // Remembered for the app session: someone who hid the dock to see the
  // whole screen shouldn't have to hide it again on every visit.
  static bool _collapsedPref = false;

  bool _collapsed = _collapsedPref;
  List<Map<String, dynamic>> _windows = const [];
  // Window ids in first-seen order. The agent lists windows in Z-order,
  // which reshuffles on every switch — a dock whose icons jump around under
  // your thumb is useless, so new windows append and the rest stay put.
  final List<Object> _order = [];
  bool _loading = false;
  // A refresh asked for while one is in flight runs again right after it:
  // the in-flight listing may predate a switch and would otherwise leave a
  // stale highlight until the next periodic tick.
  bool _refreshAgain = false;
  Object? _switchingId;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    unawaited(refresh());
    _timer = Timer.periodic(_refreshEvery, (_) => _tick());
  }

  @override
  void didUpdateWidget(AppDock oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Auto-reconnect swapped the client: re-list on the live one.
    if (!identical(oldWidget.client, widget.client)) unawaited(refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  void _tick() {
    // Only while the Screen page is really on top (not under the switcher
    // sheet or another route), the dock is showing and the app is in the
    // foreground: a phone in a pocket has no use for a fresh list every 5s.
    final lifecycle = WidgetsBinding.instance.lifecycleState;
    if (!mounted ||
        _collapsed ||
        (lifecycle != null && lifecycle != AppLifecycleState.resumed) ||
        ModalRoute.of(context)?.isCurrent != true) {
      return;
    }
    unawaited(refresh());
  }

  /// Re-lists the PC's windows now (the page calls this after the switcher
  /// closes, so the dock reflects a switch or launch immediately).
  Future<void> refresh() async {
    if (_loading) {
      _refreshAgain = true;
      return;
    }
    _loading = true;
    try {
      do {
        _refreshAgain = false;
        if (!widget.client.isConnected) return;
        try {
          final wins = await _fetchWindows(widget.client);
          if (!mounted) return;
          setState(() {
            _windows = wins;
            final ids = wins.map((w) => w['id']).whereType<Object>().toSet();
            _order.removeWhere((id) => !ids.contains(id));
            for (final id in ids) {
              if (!_order.contains(id)) _order.add(id);
            }
          });
        } catch (_) {
          // Older / non-Windows agent or a transient failure: keep the last
          // icons; the switcher button still works (and shows the error).
          // A refresh asked for meanwhile (e.g. the auto-reconnect handing
          // over a live client while this one died) still runs.
        }
      } while (_refreshAgain && mounted);
    } finally {
      _loading = false;
    }
  }

  Future<void> _switchTo(Map<String, dynamic> w) async {
    if (_switchingId != null) return;
    final id = w['id'];
    setState(() {
      _switchingId = id;
      // Move the highlight right away; the next listing confirms it.
      _windows = [
        for (final x in _windows) {...x, 'active': x['id'] == id},
      ];
    });
    try {
      await activateWindow(widget.client, w);
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _switchingId = null);
    }
    await refresh();
  }

  void _setCollapsed(bool collapsed) {
    _collapsedPref = collapsed;
    setState(() => _collapsed = collapsed);
    if (!collapsed) unawaited(refresh());
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedSwitcher(
      duration: const Duration(milliseconds: 180),
      transitionBuilder: (child, anim) => FadeTransition(
        opacity: anim,
        child: ScaleTransition(
          scale: Tween(begin: 0.9, end: 1.0).animate(anim),
          alignment: Alignment.bottomCenter,
          child: child,
        ),
      ),
      child: _collapsed ? _buildCollapsed() : _buildDock(context),
    );
  }

  Widget _buildCollapsed() {
    // Bottom-right, clear of the centered Windows 11 taskbar icons.
    return Align(
      key: const ValueKey('collapsed'),
      alignment: Alignment.bottomRight,
      child: _DockSurface(
        child: _DockButton(
          icon: Icons.keyboard_arrow_up,
          tooltip: 'Show app dock',
          size: 36,
          onTap: () => _setCollapsed(false),
        ),
      ),
    );
  }

  Widget _buildDock(BuildContext context) {
    final byId = {for (final w in _windows) w['id']: w};
    final ordered = [
      for (final id in _order)
        if (byId[id] != null) byId[id]!,
    ];
    return Align(
      key: const ValueKey('dock'),
      alignment: Alignment.bottomCenter,
      child: _DockSurface(
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Flexible + shrinkWrap: the dock hugs its icons, and once
            // there are more than fit (a 360dp phone shows ~5) the list
            // scrolls sideways instead of overflowing.
            Flexible(
              child: SizedBox(
                height: 44,
                child: ListView.builder(
                  shrinkWrap: true,
                  scrollDirection: Axis.horizontal,
                  itemCount: ordered.length,
                  itemBuilder: (context, i) => _dockIcon(context, ordered[i]),
                ),
              ),
            ),
            if (ordered.isNotEmpty)
              Container(
                width: 1,
                height: 24,
                margin: const EdgeInsets.symmetric(horizontal: 4),
                color: const Color(0xFF2A2A2A),
              ),
            _DockButton(
              icon: Icons.grid_view_rounded,
              tooltip: 'All apps',
              onTap: widget.onOpenSwitcher,
            ),
            _DockButton(
              icon: Icons.keyboard_arrow_down,
              tooltip: 'Hide dock',
              onTap: () => _setCollapsed(true),
            ),
          ],
        ),
      ),
    );
  }

  Widget _dockIcon(BuildContext context, Map<String, dynamic> w) {
    final scheme = Theme.of(context).colorScheme;
    final active = w['active'] == true;
    final icon = _AppIcons.of(w);
    final busy = _switchingId != null && _switchingId == w['id'];
    return Tooltip(
      message: '${w['title'] ?? ''}',
      child: InkWell(
        onTap: () => unawaited(_switchTo(w)),
        borderRadius: BorderRadius.circular(12),
        child: Container(
          width: 44,
          height: 44,
          decoration: BoxDecoration(
            color: active ? scheme.primary.withValues(alpha: 0.22) : null,
            borderRadius: BorderRadius.circular(12),
          ),
          child: Stack(
            alignment: Alignment.center,
            children: [
              // Minimized windows are dimmed, like a taskbar.
              Opacity(
                opacity: w['minimized'] == true ? 0.45 : 1,
                child: icon != null
                    ? Image.memory(icon,
                        width: 28,
                        height: 28,
                        gaplessPlayback: true,
                        filterQuality: FilterQuality.medium)
                    : const Icon(Icons.window_outlined,
                        size: 24, color: Colors.white70),
              ),
              if (active)
                Positioned(
                  bottom: 3,
                  child: Container(
                    width: 14,
                    height: 3,
                    decoration: BoxDecoration(
                      color: scheme.primary,
                      borderRadius: BorderRadius.circular(2),
                    ),
                  ),
                ),
              if (busy)
                const SizedBox(
                  width: 34,
                  height: 34,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The dock's translucent pill. A Material (not a plain DecoratedBox) so
/// taps show ink and the gaps between icons still absorb touches instead
/// of leaking through to the touchpad as PC clicks.
class _DockSurface extends StatelessWidget {
  const _DockSurface({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: const Color(0xE6101010),
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(18),
        side: const BorderSide(color: Color(0xFF262626)),
      ),
      child: Padding(padding: const EdgeInsets.all(4), child: child),
    );
  }
}

class _DockButton extends StatelessWidget {
  const _DockButton({
    required this.icon,
    required this.tooltip,
    required this.onTap,
    this.size = 40,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: SizedBox(
          width: size,
          height: size,
          child: Icon(icon, size: 22, color: Colors.white70),
        ),
      ),
    );
  }
}
