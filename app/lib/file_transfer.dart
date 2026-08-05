import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'relay_client.dart';

/// Size of one `fs.download` chunk; the backend caps `length` at 262144.
const int kFsChunkSize = 256 * 1024;

/// Thrown by [downloadToMemory] when the remote file grows past the caller's
/// in-memory cap.
class FileTooLargeException implements Exception {
  FileTooLargeException(this.maxBytes);

  final int maxBytes;

  @override
  String toString() =>
      'File exceeds the ${maxBytes ~/ (1024 * 1024)} MB preview limit';
}

/// Reads a remote file chunk by chunk via `fs.download`, invoking [onChunk]
/// (and awaiting it) with each decoded chunk, the bytes received so far and
/// the reported total size (0 until the backend reports one). The loop ends
/// when the backend flags `eof` or a chunk comes back empty. Returns the
/// reported total size.
///
/// This is the single implementation of the download chunking logic; both
/// the save-to-disk flow and the in-memory previews build on it.
Future<int> fetchFileChunks(
  RelayClient client,
  String path, {
  int chunkSize = kFsChunkSize,
  required FutureOr<void> Function(Uint8List chunk, int received, int total)
      onChunk,
}) async {
  var offset = 0;
  var total = 0;
  while (true) {
    final data = await client.request('fs.download', {
      'path': path,
      'offset': offset,
      'length': chunkSize,
    });
    total = (data['size'] as num?)?.toInt() ?? total;
    final bytes = base64Decode('${data['data'] ?? ''}');
    offset += bytes.length;
    if (bytes.isNotEmpty) await onChunk(bytes, offset, total);
    if (data['eof'] == true || bytes.isEmpty) break;
  }
  return total;
}

/// Reads a whole remote file into memory, throwing [FileTooLargeException]
/// once more than [maxBytes] have been received. [onProgress] receives
/// (received, total) after each chunk. The bytes never touch disk.
Future<Uint8List> downloadToMemory(
  RelayClient client,
  String path, {
  int maxBytes = 16 * 1024 * 1024,
  void Function(int received, int total)? onProgress,
}) async {
  final builder = BytesBuilder(copy: false);
  await fetchFileChunks(client, path, onChunk: (chunk, received, total) {
    if (received > maxBytes) throw FileTooLargeException(maxBytes);
    builder.add(chunk);
    onProgress?.call(received, total);
  });
  return builder.takeBytes();
}
