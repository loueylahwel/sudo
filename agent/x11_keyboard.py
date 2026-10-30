"""Typing on a Linux X11 desktop in whatever keyboard layout it has now.

pynput (the agent's input library elsewhere) reads the X keyboard map the
first time it types and keeps it, and only looks at a key's first two
levels. So once the desktop's layout changes (Plasma applies its own after
login; the owner switches AZERTY <-> QWERTY) every letter lands on the key
it had in the old layout ("a" typed as "q"), and characters behind AltGr
(@ # { } on AZERTY) come out wrong or not at all.

This types each character the way a person would on the PC's current
layout, read through XKB for every message:

- the key and level that give the character in the active layout group,
  and the modifier keys that reach that level (Shift, AltGr =
  ISO_Level3_Shift, ISO_Level5_Shift), pressed through XTest so the X
  server applies them like real key presses;
- a character the layout has no key for (é on QWERTY, emoji, Arabic on
  AZERTY) is first bound to an unused keycode, as xdotool does. The bound
  keycodes are reused least recently first, so an app still reloading the
  map for one never sees it change under it;
- Caps Lock is off while typing and back on after, so it can't turn "a"
  into "A" (or the digits row on AZERTY's caps:digits_row).

Needs only libX11 and libXtst (on every X11 desktop), through ctypes;
libxkbcommon, when present, names the characters of non-Latin layouts.
"""

import ctypes
import ctypes.util
import threading
import time

from wayland_portal import char_keysym

USE_CORE_KBD = 0x0100
CLIENT_MAP = 0x07          # XkbKeyTypesMask | XkbKeySymsMask | XkbModifierMapMask
LOCK = 0x02                # Caps Lock's real modifier bit
SCRATCH_KEYS = 8           # unused keycodes borrowed for missing characters
SCRATCH_REUSE_S = 0.2      # never rebind one typed more recently than this
# Modifier keys held to reach a level. Never Control/Alt/Super: those
# alone fire shortcuts (a lone Alt focuses menu bars).
LEVEL_KEYS = {0xffe1, 0xffe2, 0xfe03, 0xfe11}  # Shift_L/R, ISO_Level3/5_Shift


class _Mods(ctypes.Structure):
    _fields_ = [("mask", ctypes.c_ubyte), ("real_mods", ctypes.c_ubyte),
                ("vmods", ctypes.c_ushort)]


class _MapEntry(ctypes.Structure):
    _fields_ = [("active", ctypes.c_int), ("level", ctypes.c_ubyte),
                ("mods", _Mods)]


class _KeyType(ctypes.Structure):
    _fields_ = [("mods", _Mods), ("num_levels", ctypes.c_ubyte),
                ("map_count", ctypes.c_ubyte),
                ("map", ctypes.POINTER(_MapEntry)),
                ("preserve", ctypes.POINTER(_Mods)),
                ("name", ctypes.c_ulong),
                ("level_names", ctypes.POINTER(ctypes.c_ulong))]


class _SymMap(ctypes.Structure):
    _fields_ = [("kt_index", ctypes.c_ubyte * 4),
                ("group_info", ctypes.c_ubyte), ("width", ctypes.c_ubyte),
                ("offset", ctypes.c_ushort)]


class _ClientMap(ctypes.Structure):
    _fields_ = [("size_types", ctypes.c_ubyte), ("num_types", ctypes.c_ubyte),
                ("types", ctypes.POINTER(_KeyType)),
                ("size_syms", ctypes.c_ushort), ("num_syms", ctypes.c_ushort),
                ("syms", ctypes.POINTER(ctypes.c_ulong)),
                ("key_sym_map", ctypes.POINTER(_SymMap)),
                ("modmap", ctypes.POINTER(ctypes.c_ubyte))]


class _Desc(ctypes.Structure):
    _fields_ = [("dpy", ctypes.c_void_p), ("flags", ctypes.c_ushort),
                ("device_spec", ctypes.c_ushort),
                ("min_key_code", ctypes.c_ubyte),
                ("max_key_code", ctypes.c_ubyte),
                ("ctrls", ctypes.c_void_p), ("server", ctypes.c_void_p),
                ("map", ctypes.POINTER(_ClientMap)),
                ("indicators", ctypes.c_void_p), ("names", ctypes.c_void_p),
                ("compat", ctypes.c_void_p), ("geom", ctypes.c_void_p)]


class _State(ctypes.Structure):
    _fields_ = [("group", ctypes.c_ubyte), ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort),
                ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte), ("base_mods", ctypes.c_ubyte),
                ("latched_mods", ctypes.c_ubyte),
                ("locked_mods", ctypes.c_ubyte),
                ("compat_state", ctypes.c_ubyte),
                ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte),
                ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte),
                ("ptr_buttons", ctypes.c_ushort)]


def _lib(name):
    path = ctypes.util.find_library(name)
    if not path:
        raise OSError(f"lib{name} not found")
    return ctypes.CDLL(path)


def _keysym_char_fn():
    """keysym -> character. libxkbcommon knows the legacy keysyms of the
    Arabic, Cyrillic, Greek... layouts; without it Latin-1 and the
    0x1000000+U keysyms still resolve."""
    try:
        to_utf32 = _lib("xkbcommon").xkb_keysym_to_utf32
        to_utf32.argtypes = [ctypes.c_uint32]
        to_utf32.restype = ctypes.c_uint32
    except (OSError, AttributeError):
        to_utf32 = None

    def keysym_char(ks):
        if ks == 0xff0d:
            return "\n"
        if ks == 0xff09:
            return "\t"
        if to_utf32 is not None:
            code = to_utf32(ks)
        elif 0x20 <= ks <= 0x7e or 0xa0 <= ks <= 0xff:
            code = ks
        elif 0x1000100 <= ks <= 0x110ffff:
            code = ks - 0x1000000
        else:
            code = 0
        return chr(code) if code >= 0x20 and code != 0x7f else None

    return keysym_char


def _bits(mask):
    return [1 << i for i in range(8) if mask & (1 << i)]


class Typist:
    """Types text at the X11 focus. One X connection, used under a lock."""

    def __init__(self):
        x11 = _lib("X11")
        xtst = _lib("Xtst")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XkbGetMap.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        x11.XkbGetMap.restype = ctypes.POINTER(_Desc)
        x11.XkbFreeKeyboard.argtypes = [ctypes.POINTER(_Desc), ctypes.c_uint,
                                        ctypes.c_int]
        x11.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.POINTER(_State)]
        x11.XkbLockModifiers.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_uint, ctypes.c_uint]
        x11.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XPending.argtypes = [ctypes.c_void_p]
        x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_int, ctypes.c_ulong]
        self._x11, self._xtst = x11, xtst
        self._dpy = x11.XOpenDisplay(None)
        if not self._dpy:
            raise OSError("can't open the X display")
        self._keysym_char = _keysym_char_fn()
        self._lock = threading.Lock()
        # Keycodes this typist bound: {keycode: (keysym, last typed)}.
        self._scratch = {}

    def _layout(self):
        """The active group's {char: [(n mods, keycode, mask)]} (best
        first), {mod bit: keycode} for the level modifiers, the unused
        keycodes, each keycode's level-1 keysym, and the locked mods."""
        state = _State()
        self._x11.XkbGetState(self._dpy, USE_CORE_KBD, ctypes.byref(state))
        desc = self._x11.XkbGetMap(self._dpy, CLIENT_MAP, USE_CORE_KBD)
        if not desc:
            raise OSError("XkbGetMap failed")
        chars, mod_keys, free, first = {}, {}, [], {}
        try:
            d = desc.contents
            m = d.map.contents
            for kc in range(d.min_key_code, d.max_key_code + 1):
                sm = m.key_sym_map[kc]
                groups = sm.group_info & 0x0f
                if not groups:
                    if not m.modmap[kc]:
                        free.append(kc)
                    continue
                # Same rule as XkbKeycodeToKeysym for a key with fewer
                # groups than the active one.
                g = state.group
                if g >= groups:
                    action = sm.group_info & 0xc0
                    if action == 0x40:          # XkbClampIntoRange
                        g = groups - 1
                    elif action == 0x80:        # XkbRedirectIntoRange
                        g = (sm.group_info & 0x30) >> 4
                        if g >= groups:
                            g = 0
                    else:                       # XkbWrapIntoRange
                        g %= groups
                kt = m.types[sm.kt_index[g]]
                base = sm.offset + g * sm.width
                first[kc] = m.syms[base]
                if first[kc] in LEVEL_KEYS:
                    for bit in _bits(m.modmap[kc]):
                        mod_keys.setdefault(bit, kc)
                for level in range(min(kt.num_levels, sm.width)):
                    ch = self._keysym_char(m.syms[base + level])
                    if ch is None:
                        continue
                    masks = [0] if level == 0 else [
                        kt.map[i].mods.mask for i in range(kt.map_count)
                        if kt.map[i].active and kt.map[i].level == level]
                    for mask in masks:
                        chars.setdefault(ch, []).append(
                            (len(_bits(mask)), kc, mask))
        finally:
            self._x11.XkbFreeKeyboard(desc, 0, True)
        for options in chars.values():
            options.sort()
        return chars, mod_keys, free, first, state.locked_mods

    def _reach(self, options, mod_keys):
        """The first (keycode, modifier keycodes) that types it without
        Caps Lock or a modifier we can't press, or None."""
        for _, kc, mask in options:
            if mask & LOCK or any(b not in mod_keys for b in _bits(mask)):
                continue
            return kc, [mod_keys[b] for b in _bits(mask)]
        return None

    def _bind(self, ch, free, first):
        """Binds `ch` to a scratch keycode and returns it, or None."""
        ks = char_keysym(ch)
        # Ours still holding what we bound (a layout change wipes them
        # back to free; anything else now on one means it isn't ours).
        for kc, (bound, _) in list(self._scratch.items()):
            if kc not in free and first.get(kc) != bound:
                del self._scratch[kc]
        spare = [kc for kc in free if kc not in self._scratch]
        if len(self._scratch) < SCRATCH_KEYS and spare:
            kc = spare[-1]  # the highest: the least likely to be a real key
        elif self._scratch:
            kc = min(self._scratch, key=lambda k: self._scratch[k][1])
            wait = self._scratch[kc][1] + SCRATCH_REUSE_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        else:
            return None
        syms = (ctypes.c_ulong * 2)(ks, ks)
        self._x11.XChangeKeyboardMapping(self._dpy, kc, 2, syms, 1)
        self._x11.XSync(self._dpy, False)
        self._scratch[kc] = (ks, time.monotonic())
        if kc in free:
            free.remove(kc)
        first[kc] = ks
        return kc

    def _key(self, kc, down):
        self._xtst.XTestFakeKeyEvent(self._dpy, kc, down, 0)

    def type(self, text):
        """Types `text` at the focus. Characters with no key and no spare
        keycode left are skipped."""
        with self._lock:
            chars, mod_keys, free, first, locked = self._layout()
            caps = locked & LOCK
            if caps:
                self._x11.XkbLockModifiers(self._dpy, USE_CORE_KBD, LOCK, 0)
            try:
                for ch in text:
                    reach = self._reach(chars.get(ch, ()), mod_keys)
                    if reach is None:
                        if ch < " " and ch not in "\n\t":
                            continue
                        kc = self._bind(ch, free, first)
                        if kc is None:
                            continue
                        for options in chars.values():
                            options[:] = [o for o in options if o[1] != kc]
                        chars[ch] = [(0, kc, 0)]
                        reach = kc, []
                    kc, mods = reach
                    if kc in self._scratch:
                        self._scratch[kc] = (self._scratch[kc][0],
                                             time.monotonic())
                    for mod in mods:
                        self._key(mod, True)
                    self._key(kc, True)
                    self._key(kc, False)
                    for mod in reversed(mods):
                        self._key(mod, False)
            finally:
                if caps:
                    self._x11.XkbLockModifiers(self._dpy, USE_CORE_KBD,
                                               LOCK, LOCK)
                # Synced, so a Backspace sent next on pynput's own
                # connection can't overtake these keys.
                self._x11.XSync(self._dpy, False)
                self._drain()

    def _drain(self):
        """Drops the MappingNotify events every keymap change queues on
        this connection (the map is read fresh each time instead)."""
        event = ctypes.create_string_buffer(192)  # sizeof(XEvent)
        while self._x11.XPending(self._dpy):
            self._x11.XNextEvent(self._dpy, event)
