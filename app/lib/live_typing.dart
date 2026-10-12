import 'package:flutter/material.dart';

/// A text field that types on the PC as you type — no Send button.
///
/// Every edit is sent as the backspaces + text that turn what the PC got
/// so far into what the field shows now, so the phone keyboard's own
/// delete key deletes on the PC, and autocorrect / suggestion swaps are
/// replayed there as delete + retype. Its Enter key presses Enter on the
/// PC and starts a fresh line in the field.
///
/// The caret is kept at the end: the PC's caret sits after what was typed
/// here, so an edit in the middle of the field couldn't be mirrored.
/// An invisible sentinel character leads the field, so delete still
/// registers when nothing was typed yet and deletes on the PC too.
class LiveTypingField extends StatefulWidget {
  const LiveTypingField({
    super.key,
    required this.label,
    required this.onText,
    required this.onKey,
  });

  final String label;

  /// Types [text] at the PC's caret.
  final void Function(String text) onText;

  /// Presses one key on the PC: `'backspace'` or `'enter'`.
  final void Function(String key) onKey;

  @override
  State<LiveTypingField> createState() => _LiveTypingFieldState();
}

class _LiveTypingFieldState extends State<LiveTypingField> {
  static const String _sentinel = '​'; // zero-width space

  final _ctrl = TextEditingController(text: _sentinel);
  // What the PC has received since this field opened (or since Enter).
  String _sent = '';
  bool _updating = false;

  @override
  void initState() {
    super.initState();
    _ctrl.selection = const TextSelection.collapsed(offset: 1);
    _ctrl.addListener(_onChanged);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  void _onChanged() {
    if (_updating) return;
    final value = _ctrl.value;
    final hadSentinel = value.text.startsWith(_sentinel);
    final text = value.text.replaceAll(_sentinel, '');
    // Grapheme clusters, so an emoji is one backspace on the PC, not two.
    final before = _sent.characters.toList();
    final after = text.characters.toList();
    var same = 0;
    while (same < before.length &&
        same < after.length &&
        before[same] == after[same]) {
      same++;
    }
    // A deleted sentinel = delete pressed with nothing typed here left:
    // one more backspace for the text that was on the PC already.
    final deletes = before.length - same + (hadSentinel ? 0 : 1);
    for (var i = 0; i < deletes; i++) {
      widget.onKey('backspace');
    }
    final added = after.sublist(same).join();
    if (added.isNotEmpty) widget.onText(added);
    _sent = text;

    final end = text.length + 1;
    if (!hadSentinel ||
        value.text.length != end ||
        !value.selection.isCollapsed ||
        value.selection.baseOffset != end) {
      _set(text);
    }
  }

  /// Puts back the sentinel + [text] with the caret at the end.
  void _set(String text) {
    _updating = true;
    _ctrl.value = TextEditingValue(
      text: '$_sentinel$text',
      selection: TextSelection.collapsed(offset: text.length + 1),
    );
    _updating = false;
  }

  void _enter() {
    widget.onKey('enter');
    _sent = '';
    _set('');
  }

  @override
  Widget build(BuildContext context) {
    return TextField(
      controller: _ctrl,
      autofocus: true,
      textCapitalization: TextCapitalization.sentences,
      textInputAction: TextInputAction.newline,
      decoration: InputDecoration(
        labelText: widget.label,
        border: const OutlineInputBorder(),
        // Same as the keyboard's own Enter key (some keyboards show Done
        // or hide it): presses Enter on the PC and starts a fresh line.
        suffixIcon: IconButton(
          tooltip: 'Enter',
          icon: const Icon(Icons.keyboard_return),
          onPressed: _enter,
        ),
      ),
      onSubmitted: (_) => _enter(),
      // Keep the keyboard up after Enter (the default unfocuses).
      onEditingComplete: () {},
    );
  }
}
