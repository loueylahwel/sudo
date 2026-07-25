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
        // AMOLED theme: true black backgrounds (pixels off = battery saved)
        // with near-black surfaces and hairline borders for definition.
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF0D9488),
          brightness: Brightness.dark,
        ).copyWith(
          surface: Colors.black,
          surfaceContainerLowest: Colors.black,
        ),
        scaffoldBackgroundColor: Colors.black,
        appBarTheme: const AppBarTheme(
          backgroundColor: Colors.black,
          surfaceTintColor: Colors.transparent,
        ),
        cardTheme: CardThemeData(
          color: const Color(0xFF0F0F0F),
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: Color(0xFF1E1E1E)),
          ),
        ),
        dialogTheme: DialogThemeData(
          backgroundColor: const Color(0xFF101010),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(20),
            side: const BorderSide(color: Color(0xFF222222)),
          ),
        ),
        bottomSheetTheme: const BottomSheetThemeData(
          backgroundColor: Color(0xFF101010),
          surfaceTintColor: Colors.transparent,
        ),
        snackBarTheme: const SnackBarThemeData(
          backgroundColor: Color(0xFF1A1A1A),
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
