import 'dart:async';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

/// Decodes a stream's JPEG frames for display, newest first: frames that
/// arrive while one decodes replace each other, so a phone that decodes
/// slower than the PC sends drops frames instead of falling behind. The
/// picture lives in [image] (shown by [FrameImage]), so a new frame
/// repaints only the picture — not the page around it, 60 times a second.
class FrameDecoder {
  FrameDecoder({this.onDecoded});

  final ValueNotifier<ui.Image?> image = ValueNotifier(null);

  /// After each decoded frame, with its size (from the packet header, else
  /// the image) — pages relayout only when it changed.
  void Function(int w, int h)? onDecoded;

  /// The [add] tag of the picture in [image], set together with it (e.g.
  /// the part of the screen a zoomed-in frame shows): what's drawn and
  /// where it goes can never be from two different frames.
  Object? tag;

  Uint8List? _pending;
  int _pendingW = 0;
  int _pendingH = 0;
  Object? _pendingTag;
  bool _decoding = false;
  bool _disposed = false;

  /// Queues a JPEG; [w]/[h] are the header's size (0 = unknown), [tag]
  /// comes back as [tag] once this frame is the one shown.
  void add(Uint8List jpeg, int w, int h, {Object? tag}) {
    if (_disposed) return;
    _pending = jpeg;
    _pendingW = w;
    _pendingH = h;
    _pendingTag = tag;
    if (!_decoding) unawaited(_decodeLoop());
  }

  Future<void> _decodeLoop() async {
    _decoding = true;
    try {
      while (!_disposed && _pending != null) {
        final jpeg = _pending!;
        final w = _pendingW;
        final h = _pendingH;
        final frameTag = _pendingTag;
        _pending = null;
        ui.Image frame;
        try {
          final codec = await ui.instantiateImageCodec(jpeg);
          try {
            frame = (await codec.getNextFrame()).image;
          } finally {
            codec.dispose();
          }
        } catch (_) {
          continue; // corrupt frame: wait for the next one
        }
        if (_disposed) {
          frame.dispose();
          return;
        }
        onDecoded?.call(w > 0 ? w : frame.width, h > 0 ? h : frame.height);
        final old = image.value;
        tag = frameTag;
        image.value = frame;
        // RawImage paints its own clone, so ours can go now.
        old?.dispose();
      }
    } finally {
      _decoding = false;
    }
  }

  /// Drops the picture (the stream stopped for good).
  void clear() {
    _pending = null;
    tag = null;
    final old = image.value;
    image.value = null;
    old?.dispose();
  }

  /// Hands the current picture to the caller (who must dispose it) and
  /// stops decoding for good.
  ui.Image? dispose() {
    _disposed = true;
    _pending = null;
    final last = image.value;
    image.dispose();
    return last;
  }
}

/// Shows a [FrameDecoder]'s picture stretched to its box; nothing (black
/// behind it) until the first frame — no "starting" text.
class FrameImage extends StatelessWidget {
  const FrameImage(this.decoder, {super.key});

  final FrameDecoder decoder;

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: ValueListenableBuilder<ui.Image?>(
        valueListenable: decoder.image,
        builder: (context, image, _) => image == null
            ? const SizedBox.expand()
            : RawImage(
                image: image,
                fit: BoxFit.fill,
                // Frames are at most ~1280 px wide, close to the view:
                // bilinear is sharp enough and cheaper per frame than
                // mipmapped medium.
                filterQuality: FilterQuality.low,
              ),
      ),
    );
  }
}
