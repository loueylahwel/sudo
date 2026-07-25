import 'package:flutter_test/flutter_test.dart';
import 'package:sudo/util.dart';

void main() {
  test('formatBytes picks sensible units', () {
    expect(formatBytes(512), '512 B');
    expect(formatBytes(2048), '2.0 KB');
    expect(formatBytes(8 * 1024 * 1024 * 1024), '8.0 GB');
  });

  test('formatUptime renders days/hours/minutes', () {
    expect(formatUptime(90), '1m');
    expect(formatUptime(3661), '1h 1m');
    expect(formatUptime(90061), '1d 1h 1m');
  });
}
