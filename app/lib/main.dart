import 'package:flutter/material.dart';

import 'device_store.dart';
import 'pages/home_page.dart';
import 'pages/pair_page.dart';

void main() => runApp(const SudoApp());

class SudoApp extends StatelessWidget {
  const SudoApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Sudo',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF0D9488),
          brightness: Brightness.dark,
        ),
      ),
      routes: {
        '/': (_) => const LaunchGate(),
        '/pair': (_) => const PairPage(),
        '/home': (_) => const HomePage(),
      },
    );
  }
}

/// Shows the pair screen on first run, the device list once a PC is saved.
class LaunchGate extends StatelessWidget {
  const LaunchGate({super.key});

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<SavedDevice>>(
      future: DeviceStore().load(),
      builder: (context, snapshot) {
        if (!snapshot.hasData) {
          return const Scaffold(
            body: Center(child: CircularProgressIndicator()),
          );
        }
        return snapshot.data!.isEmpty ? const PairPage() : const HomePage();
      },
    );
  }
}
