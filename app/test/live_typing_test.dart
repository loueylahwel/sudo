import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sudo/live_typing.dart';

void main() {
  const s = '​'; // the field's leading sentinel
  late List<String> sent;

  Future<TextEditingController> pump(WidgetTester tester) async {
    sent = [];
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: LiveTypingField(
          label: 'Type',
          onText: (t) => sent.add('text:$t'),
          onKey: (k) => sent.add('key:$k'),
        ),
      ),
    ));
    return tester.widget<TextField>(find.byType(TextField)).controller!;
  }

  void type(TextEditingController c, String text) {
    c.value = TextEditingValue(
      text: '$s$text',
      selection: TextSelection.collapsed(offset: text.length + 1),
    );
  }

  testWidgets('typing and deleting are mirrored as they happen',
      (tester) async {
    final c = await pump(tester);
    type(c, 'h');
    type(c, 'he');
    type(c, 'hel');
    type(c, 'he'); // phone delete key
    expect(sent, ['text:h', 'text:e', 'text:l', 'key:backspace']);
  });

  testWidgets('autocorrect swaps become delete + retype', (tester) async {
    final c = await pump(tester);
    type(c, 'teh');
    sent.clear();
    type(c, 'the ');
    expect(sent, ['key:backspace', 'key:backspace', 'text:he ']);
  });

  testWidgets('delete with nothing typed still deletes on the PC',
      (tester) async {
    final c = await pump(tester);
    c.value = const TextEditingValue(
        text: '', selection: TextSelection.collapsed(offset: 0));
    expect(sent, ['key:backspace']);
    // The sentinel is back, so the next delete registers too.
    expect(c.text, s);
    c.value = const TextEditingValue(
        text: '', selection: TextSelection.collapsed(offset: 0));
    expect(sent, ['key:backspace', 'key:backspace']);
  });

  testWidgets('an emoji is one backspace', (tester) async {
    final c = await pump(tester);
    type(c, 'ok👍');
    sent.clear();
    type(c, 'ok');
    expect(sent, ['key:backspace']);
  });

  testWidgets('Enter presses Enter and starts a fresh line', (tester) async {
    final c = await pump(tester);
    type(c, 'hi');
    await tester.showKeyboard(find.byType(TextField));
    await tester.testTextInput.receiveAction(TextInputAction.newline);
    expect(sent.last, 'key:enter');
    expect(c.text, s);
    sent.clear();
    // Deleting now must not reach back into the line before Enter.
    c.value = const TextEditingValue(
        text: '', selection: TextSelection.collapsed(offset: 0));
    expect(sent, ['key:backspace']);
  });

  testWidgets('the Enter button works like the keyboard Enter key',
      (tester) async {
    final c = await pump(tester);
    type(c, 'hi');
    sent.clear();
    await tester.tap(find.byTooltip('Enter'));
    expect(sent, ['key:enter']);
    expect(c.text, s);
  });

  testWidgets('the caret is kept at the end', (tester) async {
    final c = await pump(tester);
    type(c, 'abc');
    c.selection = const TextSelection.collapsed(offset: 1);
    expect(c.selection.baseOffset, 4);
  });
}
