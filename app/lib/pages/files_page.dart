import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:path_provider/path_provider.dart';

import '../file_transfer.dart';
import '../relay_client.dart';
import '../util.dart';
import 'file_preview_page.dart';

class FsEntry {
  FsEntry({
    required this.name,
    required this.path,
    required this.type,
    required this.size,
    required this.mtime,
  });

  final String name;
  final String path;
  final String type; // "dir" | "file" | "drive"
  final num size;
  final dynamic mtime;

  bool get isDirLike => type == 'dir' || type == 'drive';

  factory FsEntry.fromJson(Map<String, dynamic> json) => FsEntry(
        name: json['name'] as String? ?? '',
        path: json['path'] as String? ?? '',
        type: json['type'] as String? ?? 'file',
        size: json['size'] as num? ?? 0,
        mtime: json['mtime'],
      );
}

/// A well-known folder on the PC (Home, Desktop, ..., Drives) as returned by
/// `fs.shortcuts`. An empty [path] means the drive list.
class FsShortcut {
  FsShortcut({required this.name, required this.path});

  final String name;
  final String path;

  factory FsShortcut.fromJson(Map<String, dynamic> json) => FsShortcut(
        name: json['name'] as String? ?? '',
        path: json['path'] as String? ?? '',
      );
}

class FilesPage extends StatefulWidget {
  const FilesPage({super.key, required this.client});

  final RelayClient client;

  @override
  State<FilesPage> createState() => _FilesPageState();
}

class _FilesPageState extends State<FilesPage> {
  String _path = ''; // Empty = drive list.
  List<FsEntry> _entries = [];
  List<FsShortcut> _shortcuts = [];
  bool _loading = true;
  String? _error;

  static const _downloadChannel = MethodChannel('pcocket/downloads');

  @override
  void initState() {
    super.initState();
    unawaited(_init());
  }

  /// Fetches the PC's well-known folders for the shortcut chips, then opens
  /// the Home folder (falling back to the drive list).
  Future<void> _init() async {
    var startPath = '';
    try {
      final data = await widget.client.request('fs.shortcuts');
      final shortcuts = (data['shortcuts'] as List? ?? [])
          .map((e) => FsShortcut.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
      if (!mounted) return;
      setState(() => _shortcuts = shortcuts);
      for (final s in shortcuts) {
        if (s.name.toLowerCase() == 'home') {
          startPath = s.path;
          break;
        }
      }
    } catch (_) {
      // Shortcuts unavailable; fall back to the drive list.
    }
    unawaited(_load(startPath));
  }

  Future<void> _load(String path) async {
    setState(() {
      _path = path;
      _loading = true;
      _error = null;
    });
    try {
      final data = await widget.client.request('fs.list', {'path': path});
      final entries = (data['entries'] as List? ?? [])
          .map((e) => FsEntry.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
      if (!mounted) return;
      setState(() {
        _entries = entries;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _loading = false;
      });
    }
  }

  String _join(String base, String name) {
    if (base.isEmpty) return name;
    if (base.endsWith('\\') || base.endsWith('/')) return '$base$name';
    return '$base\\$name';
  }

  List<String> get _segments => _path
      .split(RegExp('[\\\\/]+'))
      .where((s) => s.isNotEmpty)
      .toList();

  String _pathForSegment(int index) {
    final segs = _segments.sublist(0, index + 1);
    var p = segs.join('\\');
    if (index == 0 && segs[0].endsWith(':')) p = '$p\\';
    return p;
  }

  IconData _iconFor(FsEntry e) {
    switch (e.type) {
      case 'drive':
        return Icons.storage;
      case 'dir':
        return Icons.folder;
      default:
        return Icons.insert_drive_file_outlined;
    }
  }

  String _subtitleFor(FsEntry e) {
    final parts = <String>[];
    if (!e.isDirLike) parts.add(formatBytes(e.size));
    final m = e.mtime;
    if (m is num) {
      final dt = DateTime.fromMillisecondsSinceEpoch(
          (m > 1e12 ? m : m * 1000).round());
      parts.add('${dt.year}-${_two(dt.month)}-${_two(dt.day)} '
          '${_two(dt.hour)}:${_two(dt.minute)}');
    } else if (m is String && m.isNotEmpty) {
      parts.add(m);
    }
    return parts.join('  ·  ');
  }

  String _two(int v) => v.toString().padLeft(2, '0');

  // ---- actions ----

  Future<void> _download(FsEntry entry) async {
    final progress = ValueNotifier<double>(0);
    unawaited(showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => ValueListenableBuilder<double>(
        valueListenable: progress,
        builder: (ctx, value, _) => AlertDialog(
          title: Text('Downloading ${entry.name}'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              LinearProgressIndicator(value: value <= 0 ? null : value),
              const SizedBox(height: 8),
              Text('${(value * 100).toStringAsFixed(0)}%'),
            ],
          ),
        ),
      ),
    ));
    String? savedMessage;
    Object? failure;
    File? tempFile;
    try {
      final tempDir = await getTemporaryDirectory();
      tempFile =
          File('${tempDir.path}${Platform.pathSeparator}${entry.name}');
      if (await tempFile.exists()) await tempFile.delete();
      final file = tempFile;
      await fetchFileChunks(widget.client, entry.path,
          onChunk: (chunk, received, total) async {
        await file.writeAsBytes(chunk, mode: FileMode.append, flush: true);
        progress.value =
            total > 0 ? (received / total).clamp(0.0, 1.0) : 0;
      });
      savedMessage = await _saveToDownloads(tempFile, entry.name);
    } catch (e) {
      failure = e;
    } finally {
      if (tempFile != null && await tempFile.exists()) {
        try {
          await tempFile.delete();
        } catch (_) {
          // Temp file already moved/deleted; nothing to do.
        }
      }
    }
    if (!mounted) return;
    Navigator.of(context).pop(); // Close the progress dialog.
    if (savedMessage != null) {
      showMessage(context, savedMessage);
    } else {
      showError(context, failure ?? 'Download failed');
    }
  }

  /// Moves a finished download into the phone's public Download folder
  /// (`Download/PCocket/`) via the native MediaStore bridge in
  /// MainActivity. Falls back to the app documents directory if the bridge
  /// is unavailable or fails.
  Future<String> _saveToDownloads(File tempFile, String name) async {
    try {
      final ok = await _downloadChannel.invokeMethod<bool>(
        'saveToDownloads',
        {'name': name, 'path': tempFile.path},
      );
      if (ok != true) throw Exception('native save returned $ok');
      return 'Saved to Downloads';
    } catch (_) {
      final docs = await getApplicationDocumentsDirectory();
      final dir =
          Directory('${docs.path}${Platform.pathSeparator}downloads');
      await dir.create(recursive: true);
      final dest = File('${dir.path}${Platform.pathSeparator}$name');
      if (await dest.exists()) await dest.delete();
      await tempFile.copy(dest.path);
      return 'Saved to ${dest.path}';
    }
  }

  Future<void> _upload() async {
    if (_path.isEmpty) {
      showMessage(context, 'Open a folder first');
      return;
    }
    final result = await FilePicker.platform.pickFiles();
    if (result == null || result.files.isEmpty) return;
    final picked = result.files.single;
    final srcPath = picked.path;
    if (srcPath == null) {
      if (mounted) showError(context, 'Could not read the picked file');
      return;
    }
    if (!mounted) return;
    final progress = ValueNotifier<double>(0);
    unawaited(showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => ValueListenableBuilder<double>(
        valueListenable: progress,
        builder: (ctx, value, _) => AlertDialog(
          title: Text('Uploading ${picked.name}'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              LinearProgressIndicator(value: value <= 0 ? null : value),
              const SizedBox(height: 8),
              Text('${(value * 100).toStringAsFixed(0)}%'),
            ],
          ),
        ),
      ),
    ));
    Object? failure;
    try {
      final bytes = await File(srcPath).readAsBytes();
      final target = _join(_path, picked.name);
      var offset = 0;
      do {
        final end = math.min(offset + kFsChunkSize, bytes.length);
        await widget.client.request('fs.upload', {
          'path': target,
          'data': base64Encode(bytes.sublist(offset, end)),
          'offset': offset,
          'append': offset > 0,
        });
        offset = end;
        progress.value =
            bytes.isEmpty ? 1 : (offset / bytes.length).clamp(0.0, 1.0);
      } while (offset < bytes.length);
    } catch (e) {
      failure = e;
    }
    if (!mounted) return;
    Navigator.of(context).pop(); // Close the progress dialog.
    if (failure != null) {
      var message = '$failure';
      final lower = message.toLowerCase();
      if (lower.contains('permission') ||
          lower.contains('denied') ||
          lower.contains('access') ||
          lower.contains('eperm') ||
          lower.contains('eacces')) {
        message = '$message — pick a folder inside Home instead';
      }
      showError(context, message);
    } else {
      showMessage(context, 'Uploaded ${picked.name}');
      unawaited(_load(_path));
    }
  }

  Future<void> _delete(FsEntry entry) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Delete ${entry.name}?'),
        content: Text(entry.isDirLike
            ? 'This deletes the folder and everything inside it.'
            : 'This deletes the file permanently.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await widget.client.request('fs.delete', {'path': entry.path});
      if (mounted) {
        showMessage(context, 'Deleted ${entry.name}');
        unawaited(_load(_path));
      }
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  Future<void> _mkdir() async {
    if (_path.isEmpty) {
      showMessage(context, 'Open a folder first');
      return;
    }
    final ctrl = TextEditingController();
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('New folder'),
        content: TextField(
          controller: ctrl,
          autofocus: true,
          decoration: const InputDecoration(
            labelText: 'Folder name',
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
            child: const Text('Create'),
          ),
        ],
      ),
    );
    if (name == null || name.trim().isEmpty) return;
    try {
      await widget.client
          .request('fs.mkdir', {'path': _join(_path, name.trim())});
      if (mounted) unawaited(_load(_path));
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  /// Tapping a previewable file (image, or text up to 2MB) opens it in the
  /// in-app preview page; anything else gets the action menu.
  void _onFileTap(FsEntry entry) {
    if (isPreviewable(entry.name, entry.size)) {
      Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => FilePreviewPage(
          client: widget.client,
          name: entry.name,
          path: entry.path,
          size: entry.size,
          onDownload: () => unawaited(_download(entry)),
        ),
      ));
    } else {
      _showEntryMenu(entry);
    }
  }

  void _showEntryMenu(FsEntry entry) {
    showModalBottomSheet<void>(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (!entry.isDirLike)
              ListTile(
                leading: const Icon(Icons.download),
                title: const Text('Download'),
                onTap: () {
                  Navigator.pop(ctx);
                  unawaited(_download(entry));
                },
              ),
            ListTile(
              leading: const Icon(Icons.delete_outline),
              title: const Text('Delete'),
              onTap: () {
                Navigator.pop(ctx);
                unawaited(_delete(entry));
              },
            ),
          ],
        ),
      ),
    );
  }

  // ---- build ----

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Files'),
        actions: [
          IconButton(
            tooltip: 'Upload here',
            icon: const Icon(Icons.upload_file),
            onPressed: _upload,
          ),
          IconButton(
            tooltip: 'New folder',
            icon: const Icon(Icons.create_new_folder_outlined),
            onPressed: _mkdir,
          ),
        ],
      ),
      body: Column(
        children: [
          if (_shortcuts.isNotEmpty) _buildShortcuts(),
          _buildBreadcrumb(),
          const Divider(height: 1),
          Expanded(child: _buildBody()),
        ],
      ),
    );
  }

  IconData _shortcutIcon(String name) {
    switch (name.toLowerCase()) {
      case 'home':
        return Icons.home_outlined;
      case 'desktop':
        return Icons.desktop_windows_outlined;
      case 'documents':
        return Icons.description_outlined;
      case 'downloads':
        return Icons.download_outlined;
      case 'pictures':
        return Icons.photo_library_outlined;
      case 'videos':
        return Icons.video_library_outlined;
      case 'drives':
        return Icons.storage;
      default:
        return Icons.folder_special_outlined;
    }
  }

  Widget _buildShortcuts() {
    return SizedBox(
      height: 48,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        itemCount: _shortcuts.length,
        separatorBuilder: (_, _) => const SizedBox(width: 8),
        itemBuilder: (context, i) {
          final s = _shortcuts[i];
          return ActionChip(
            avatar: Icon(_shortcutIcon(s.name), size: 18),
            label: Text(s.name),
            onPressed: () => unawaited(_load(s.path)),
          );
        },
      ),
    );
  }

  Widget _buildBreadcrumb() {
    final segments = _segments;
    return SizedBox(
      height: 48,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 8),
        children: [
          ActionChip(
            avatar: const Icon(Icons.storage, size: 18),
            label: const Text('Drives'),
            onPressed: () => unawaited(_load('')),
          ),
          for (var i = 0; i < segments.length; i++) ...[
            const Icon(Icons.chevron_right, size: 18),
            ActionChip(
              label: Text(segments[i]),
              onPressed: () => unawaited(_load(_pathForSegment(i))),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildBody() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_error!, textAlign: TextAlign.center),
            const SizedBox(height: 12),
            FilledButton.tonal(
              onPressed: () => unawaited(_load(_path)),
              child: const Text('Retry'),
            ),
          ],
        ),
      );
    }
    return RefreshIndicator(
      onRefresh: () => _load(_path),
      child: _entries.isEmpty
          ? ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              children: const [
                SizedBox(height: 120),
                Center(child: Text('Empty folder')),
              ],
            )
          : ListView.builder(
              physics: const AlwaysScrollableScrollPhysics(),
              itemCount: _entries.length,
              itemBuilder: (context, i) {
                final e = _entries[i];
                return ListTile(
                  leading: Icon(_iconFor(e)),
                  title: Text(e.name, overflow: TextOverflow.ellipsis),
                  subtitle: _subtitleFor(e).isEmpty
                      ? null
                      : Text(_subtitleFor(e)),
                  trailing: IconButton(
                    icon: const Icon(Icons.more_vert),
                    onPressed: () => _showEntryMenu(e),
                  ),
                  onTap: e.isDirLike
                      ? () => unawaited(_load(e.path))
                      : () => _onFileTap(e),
                  onLongPress: () => _showEntryMenu(e),
                );
              },
            ),
    );
  }
}
