import 'package:flutter/material.dart';

import '../device_store.dart';
import '../discovery.dart';
import '../relay_client.dart';
import 'home_page.dart';

class PairPage extends StatefulWidget {
  const PairPage({super.key});

  @override
  State<PairPage> createState() => _PairPageState();
}

class _PairPageState extends State<PairPage> {
  final _addrCtrl = TextEditingController();
  final _phoneNameCtrl = TextEditingController();
  bool _busy = false;
  String? _error;

  // True while the relay waits for the user to Allow this phone on the PC
  // (agents with require_approval answer an unknown phone with
  // "approval.pending" and show an Allow/Deny dialog there).
  bool _approvalPending = false;

  // Discovery state: null = not searched yet this session.
  bool _scanning = false;
  List<DiscoveredPc>? _found;

  bool _manualVisible = false;

  @override
  void initState() {
    super.initState();
    DeviceStore.phoneName().then((n) {
      if (mounted) _phoneNameCtrl.text = n;
    });
  }

  @override
  void dispose() {
    _addrCtrl.dispose();
    _phoneNameCtrl.dispose();
    super.dispose();
  }

  Future<void> _findPc() async {
    setState(() {
      _scanning = true;
      _found = null;
      _error = null;
    });
    final found = await discoverPcs();
    if (!mounted) return;
    setState(() {
      _scanning = false;
      _found = found;
    });
  }

  /// Tapping a discovered PC pairs immediately, without asking for a code.
  Future<void> _onPcTap(DiscoveredPc pc) => _pairWith(pc.relayUrl);

  /// Builds a `ws://host:port` URL from user input (`ip`, `ip:port` or a
  /// `ws://` URL); the port defaults to 8080. Null when the input is empty.
  static String? _wsUrlFromInput(String input) {
    var s = input.trim();
    if (s.startsWith('ws://')) s = s.substring('ws://'.length);
    final slash = s.indexOf('/');
    if (slash >= 0) s = s.substring(0, slash);
    if (s.isEmpty) return null;
    if (!s.contains(':')) s = '$s:8080';
    return 'ws://$s';
  }

  Future<void> _pairManually() {
    final relayUrl = _wsUrlFromInput(_addrCtrl.text);
    if (relayUrl == null) {
      setState(() => _error = 'Enter the PC address');
      return Future.value();
    }
    return _pairWith(relayUrl);
  }

  Future<void> _pairWith(String relayUrl, [String? code]) async {
    setState(() {
      _busy = true;
      _error = null;
      _approvalPending = false;
    });
    final client = RelayClient();
    try {
      // If this PC was paired before, send its saved trust token so the
      // agent skips the on-PC approval prompt.
      var token = '';
      for (final d in await DeviceStore().load()) {
        if (d.relayUrl == relayUrl && d.token.isNotEmpty) {
          token = d.token;
          break;
        }
      }
      final result = await connectAndPair(client,
          relayUrl: relayUrl,
          code: code,
          token: token,
          name: await DeviceStore.phoneName(),
          timeout: const Duration(seconds: 5),
          onApprovalPending: () {
            if (mounted) setState(() => _approvalPending = true);
          });
      final name = result.paired['name'] as String? ?? relayUrl;
      // Nothing is saved before this point: only a successful pair
      // (`paired`) persists the device. `token` is the trust credential for
      // prompt-free re-pairing; older agents send none, then '' is stored.
      await DeviceStore().save(SavedDevice(
          relayUrl: relayUrl,
          code: code ?? '',
          name: name,
          token: result.paired['token'] as String? ?? ''));
      await client.disconnect();
      if (!mounted) return;
      if (Navigator.canPop(context)) {
        Navigator.pop(context, true);
      } else {
        Navigator.pushReplacement(
            context, MaterialPageRoute(builder: (_) => const HomePage()));
      }
    } on ChooseDeviceException catch (e) {
      // Several agents are online: let the user pick one and re-pair with
      // its code.
      await client.disconnect();
      if (!mounted) return;
      setState(() {
        _busy = false;
        _approvalPending = false;
      });
      final chosen = await _chooseDevice(e.devices);
      if (chosen == null || !mounted) return;
      await _pairWith(relayUrl, chosen.code);
    } catch (e) {
      await client.disconnect();
      if (!mounted) return;
      setState(() {
        _busy = false;
        _approvalPending = false;
        // Shows the agent's exact reason, e.g. "denied on this PC".
        _error = '$e';
      });
    }
  }

  /// Shown when a codeless pair matched several online PCs: lists the
  /// candidates the relay returned and resolves with the one the user taps.
  Future<ChooseDevice?> _chooseDevice(List<ChooseDevice> devices) {
    return showDialog<ChooseDevice>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Several PCs are online'),
        content: SizedBox(
          width: double.maxFinite,
          child: ListView(
            shrinkWrap: true,
            children: [
              for (final d in devices)
                ListTile(
                  leading: const Icon(Icons.computer),
                  title: Text(d.name.isEmpty ? d.code : d.name),
                  onTap: () => Navigator.pop(ctx, d),
                ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('Cancel'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Pair a PC')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: [
            Icon(Icons.devices, size: 64, color: theme.colorScheme.primary),
            const SizedBox(height: 16),
            Text(
              'Connect to your PC',
              style: theme.textTheme.headlineSmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              'Make sure your phone and PC are on the same Wi-Fi network, '
              'then find your PC and tap it to pair.',
              style: theme.textTheme.bodyMedium,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _phoneNameCtrl,
              textAlign: TextAlign.center,
              decoration: const InputDecoration(
                labelText: 'This phone’s name (shown on the PC)',
                border: OutlineInputBorder(),
                isDense: true,
              ),
              onChanged: DeviceStore.setPhoneName,
            ),
            const SizedBox(height: 32),
            FilledButton.icon(
              onPressed: (_busy || _scanning) ? null : _findPc,
              icon: const Icon(Icons.wifi_find, size: 28),
              label: const Text('Find my PC', style: TextStyle(fontSize: 18)),
              style: FilledButton.styleFrom(
                minimumSize: const Size.fromHeight(64),
              ),
            ),
            const SizedBox(height: 24),
            if (_scanning)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 16),
                child: Column(
                  children: [
                    CircularProgressIndicator(),
                    SizedBox(height: 16),
                    Text('Looking for PCs on your network…'),
                  ],
                ),
              )
            else if (_found != null) ...[
              if (_found!.isEmpty)
                _buildNoPcFound(theme)
              else
                ..._found!.map(_buildPcCard),
            ],
            if (_approvalPending)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 16),
                child: Column(
                  children: [
                    CircularProgressIndicator(),
                    SizedBox(height: 16),
                    Text('Waiting for approval on the PC…'),
                    SizedBox(height: 4),
                    Text('Tap Allow in the dialog shown on your PC.'),
                  ],
                ),
              ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Text(
                _error!,
                style: TextStyle(color: theme.colorScheme.error),
                textAlign: TextAlign.center,
              ),
            ],
            const SizedBox(height: 16),
            Center(
              child: TextButton(
                onPressed: _busy
                    ? null
                    : () =>
                        setState(() => _manualVisible = !_manualVisible),
                child: Text(_manualVisible
                    ? 'Hide manual entry'
                    : 'Enter IP manually'),
              ),
            ),
            if (_manualVisible) ...[
              const SizedBox(height: 8),
              TextField(
                controller: _addrCtrl,
                enabled: !_busy,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(
                  labelText: 'PC address',
                  hintText: '192.168.1.10 or 192.168.1.10:8080',
                  prefixIcon: Icon(Icons.dns_outlined),
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),
              FilledButton.tonalIcon(
                onPressed: _busy ? null : _pairManually,
                icon: _busy
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.link),
                label: Text(_busy ? 'Pairing…' : 'Pair'),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildPcCard(DiscoveredPc pc) {
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: ListTile(
        leading: const Icon(Icons.computer, size: 36),
        title: Text(pc.name),
        subtitle: Text(pc.ip),
        trailing: const Icon(Icons.chevron_right),
        onTap: _busy ? null : () => _onPcTap(pc),
      ),
    );
  }

  Widget _buildNoPcFound(ThemeData theme) {
    return Column(
      children: [
        Icon(Icons.search_off, size: 40, color: theme.hintColor),
        const SizedBox(height: 8),
        Text('No PC found on this network',
            style: theme.textTheme.titleMedium, textAlign: TextAlign.center),
        const SizedBox(height: 4),
        Text(
          'Check that the PC agent is running and both devices are on the '
          'same Wi-Fi network.',
          style: theme.textTheme.bodySmall,
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: _busy ? null : _findPc,
          icon: const Icon(Icons.refresh),
          label: const Text('Retry'),
        ),
      ],
    );
  }
}
