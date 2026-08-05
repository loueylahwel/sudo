import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../file_transfer.dart';
import '../relay_client.dart';
import '../util.dart';

/// Extensions that open in the image preview (matched lowercase).
const Set<String> kImagePreviewExts = {
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp',
};

/// Extensions that open in the text preview (matched lowercase).
const Set<String> kTextPreviewExts = {
  'txt', 'md', 'log', 'json', 'yaml', 'yml', 'xml', 'csv', 'ini', 'cfg',
  'dart', 'py', 'js', 'ts', 'html', 'css', 'kt', 'java', 'c', 'cpp', 'h',
  'rs', 'go', 'sh', 'bat', 'ps1',
};

/// Text files larger than this are not previewed; they fall back to the
/// download flow.
const int kTextPreviewMaxBytes = 2 * 1024 * 1024;

/// Extension-less files are treated as text only below this size.
const int kNoExtTextMaxBytes = 256 * 1024;

/// Hard cap for any preview held in memory, regardless of type.
const int kMaxPreviewBytes = 16 * 1024 * 1024;

/// Lowercase extension of [name] without the dot; '' when there is none
/// (dotfiles like `.gitignore` count as extension-less).
String fileExtension(String name) {
  final i = name.lastIndexOf('.');
  if (i <= 0 || i == name.length - 1) return '';
  return name.substring(i + 1).toLowerCase();
}

/// Whether [name] is an image the preview page can render.
bool isImageFile(String name) => kImagePreviewExts.contains(fileExtension(name));

/// Whether [name] (of [size] bytes) is a text file the preview page can
/// show. Extension-less files qualify only under [kNoExtTextMaxBytes].
bool isTextFile(String name, num size) {
  final ext = fileExtension(name);
  if (kTextPreviewExts.contains(ext)) return true;
  return ext.isEmpty && size < kNoExtTextMaxBytes;
}

/// Whether a tapped file should open in [FilePreviewPage] instead of the
/// action menu: images always (oversized ones hit the in-page too-large
/// state), text only up to [kTextPreviewMaxBytes].
bool isPreviewable(String name, num size) =>
    isImageFile(name) ||
    (isTextFile(name, size) && size <= kTextPreviewMaxBytes);

enum _Kind { image, text }

enum _Status { loading, ready, tooLarge, error }

/// Internal signal used to abort the chunk loop when the page is closed
/// mid-download.
class _PreviewCancelled implements Exception {}

/// In-app preview of a single remote file. The file is streamed into memory
/// via chunked `fs.download` calls (never written to phone storage) and shown
/// as a zoomable image or selectable monospace text. [onDownload] runs the
/// app's regular save-to-Download-folder flow for users who do want the file.
class FilePreviewPage extends StatefulWidget {
  const FilePreviewPage({
    super.key,
    required this.client,
    required this.name,
    required this.path,
    required this.size,
    required this.onDownload,
  });

  final RelayClient client;
  final String name;
  final String path;
  final num size;

  /// Kicks off the existing download flow for this file (progress dialog,
  /// save into the phone's Download folder).
  final VoidCallback onDownload;

  @override
  State<FilePreviewPage> createState() => _FilePreviewPageState();
}

class _FilePreviewPageState extends State<FilePreviewPage> {
  late final _Kind _kind =
      isImageFile(widget.name) ? _Kind.image : _Kind.text;
  final TransformationController _transformCtrl = TransformationController();
  TapDownDetails? _doubleTapDetails;

  _Status _status = _Status.loading;
  Object? _error;
  int _received = 0;
  int _total = 0;
  Uint8List? _bytes;
  String? _text;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _transformCtrl.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _status = _Status.loading;
      _error = null;
      _received = 0;
      _total = widget.size.toInt();
      _bytes = null;
      _text = null;
    });
    try {
      final bytes = await downloadToMemory(
        widget.client,
        widget.path,
        maxBytes: kMaxPreviewBytes,
        onProgress: (received, total) {
          if (!mounted) throw _PreviewCancelled();
          setState(() {
            _received = received;
            _total = total;
          });
        },
      );
      if (!mounted) return;
      setState(() {
        _bytes = bytes;
        if (_kind == _Kind.text) {
          _text = utf8.decode(bytes, allowMalformed: true);
        }
        _status = _Status.ready;
      });
    } on _PreviewCancelled {
      // Page was closed mid-download; drop whatever arrived.
    } on FileTooLargeException {
      if (mounted) setState(() => _status = _Status.tooLarge);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e;
        _status = _Status.error;
      });
    }
  }

  /// Double-tap toggles between 1x (fit) and 3x zoom centered on the tap.
  void _toggleZoom() {
    if (_transformCtrl.value.getMaxScaleOnAxis() > 1.0) {
      _transformCtrl.value = Matrix4.identity();
      return;
    }
    final pos = _doubleTapDetails?.localPosition;
    if (pos == null) {
      _transformCtrl.value = Matrix4.identity()
        ..scaleByDouble(3.0, 3.0, 3.0, 1.0);
    } else {
      _transformCtrl.value = Matrix4.identity()
        ..translateByDouble(-pos.dx * 2.0, -pos.dy * 2.0, 0.0, 1.0)
        ..scaleByDouble(3.0, 3.0, 3.0, 1.0);
    }
  }

  @override
  Widget build(BuildContext context) {
    final isImage = _kind == _Kind.image;
    final body = _buildBody(context);
    return Scaffold(
      backgroundColor: isImage ? Colors.black : null,
      appBar: AppBar(
        backgroundColor: isImage ? Colors.black : null,
        foregroundColor: isImage ? Colors.white : null,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(widget.name, overflow: TextOverflow.ellipsis),
            Text(
              formatBytes(widget.size),
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: isImage
                        ? Colors.white70
                        : Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Download',
            icon: const Icon(Icons.download),
            onPressed: widget.onDownload,
          ),
        ],
      ),
      // On the AMOLED-black image background every secondary label and icon
      // must stay light; override the defaults for the whole body subtree.
      body: isImage
          ? IconTheme(
              data: const IconThemeData(color: Colors.white70),
              child: DefaultTextStyle(
                style: const TextStyle(color: Colors.white70),
                child: body,
              ),
            )
          : body,
    );
  }

  Widget _buildBody(BuildContext context) {
    switch (_status) {
      case _Status.loading:
        return Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              CircularProgressIndicator(
                value:
                    _total > 0 ? (_received / _total).clamp(0.0, 1.0) : null,
              ),
              const SizedBox(height: 16),
              Text(
                _total > 0
                    ? 'Loading ${formatBytes(_received)} / ${formatBytes(_total)}'
                    : 'Loading ${formatBytes(_received)}',
              ),
            ],
          ),
        );
      case _Status.tooLarge:
        return _MessageState(
          icon: Icons.file_present_outlined,
          title: 'File too large to preview',
          detail:
              'Previews are limited to ${formatBytes(kMaxPreviewBytes)}. Download the file instead.',
          actionIcon: Icons.download,
          actionLabel: 'Download',
          onAction: widget.onDownload,
        );
      case _Status.error:
        return _MessageState(
          icon: Icons.error_outline,
          title: 'Could not load the file',
          detail: '$_error',
          actionIcon: Icons.refresh,
          actionLabel: 'Retry',
          onAction: () => unawaited(_load()),
        );
      case _Status.ready:
        return _kind == _Kind.image ? _buildImage() : _buildText();
    }
  }

  Widget _buildImage() {
    return GestureDetector(
      onDoubleTapDown: (details) => _doubleTapDetails = details,
      onDoubleTap: _toggleZoom,
      child: InteractiveViewer(
        transformationController: _transformCtrl,
        minScale: 1.0,
        maxScale: 8.0,
        child: Center(
          child: Image.memory(
            _bytes!,
            fit: BoxFit.contain,
            errorBuilder: (context, error, stackTrace) => _MessageState(
              icon: Icons.broken_image_outlined,
              title: 'Could not decode the image',
              detail: '$error',
              actionIcon: Icons.refresh,
              actionLabel: 'Retry',
              onAction: () => unawaited(_load()),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildText() {
    return Scrollbar(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(12),
        child: SizedBox(
          width: double.infinity,
          child: SelectableText(
            _text ?? '',
            style: const TextStyle(
              fontFamily: 'monospace',
              fontSize: 13,
              height: 1.35,
            ),
          ),
        ),
      ),
    );
  }
}

/// Centered icon + title + detail + single action button, used for the
/// loading-failure and too-large states.
class _MessageState extends StatelessWidget {
  const _MessageState({
    required this.icon,
    required this.title,
    required this.detail,
    required this.actionIcon,
    required this.actionLabel,
    required this.onAction,
  });

  final IconData icon;
  final String title;
  final String detail;
  final IconData actionIcon;
  final String actionLabel;
  final VoidCallback onAction;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 48),
            const SizedBox(height: 12),
            Text(
              title,
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: DefaultTextStyle.of(context).style.color,
                  ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(detail, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton.icon(
              onPressed: onAction,
              icon: Icon(actionIcon),
              label: Text(actionLabel),
            ),
          ],
        ),
      ),
    );
  }
}
