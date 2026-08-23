"""
Copyright (C) 2026  Candy Arts - https://candy-arts.com

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

"""
Window Resizer
--------------
Adds an N-panel tab listing every Blender window with a monitor, a width and a
height, and a "Set" button.

Pressing "Set" runs four steps:

  1. drag the window so its CENTER lands on a corner of the target monitor,
     which leaves it overlapping two or more monitors
  2. wait for the window manager to finish the move
  3. resize it
  4. drag it to the top-left corner of the target monitor

Steps 1 and 4 are performed by synthesizing a titlebar drag with XTest, which
the window manager treats as a user-initiated interactive move and therefore
does not clamp to a single monitor. Step 3 is an ordinary resize request, which
the window manager measures against every monitor the window is currently on -
hence step 1.

Layouts (a monitor + size + include flag for every window) can be saved under a
name to a JSON file in Blender's config directory, so they survive restarts.
One layout can be marked as the startup layout; it is applied automatically a
moment after Blender finishes launching.

Blender windows are matched to X11 windows by geometry, which means the match
has to be made before anything moves: Blender only refreshes the size and
position of its own windows when its event loop runs, and that is blocked for
the whole of a Set. The mapping is therefore resolved once per run and the
resulting window ids are passed down.

Each window can be placed at the top-left of its monitor, at an offset from
that corner, or at an absolute desktop coordinate, which is what makes split
screen layouts possible. The final placement is a measure-and-correct loop, so
a window manager that snaps or resists at edges gets corrected rather than
quietly leaving the window a few pixels out.

X11 only (Xorg or XWayland). Pure ctypes, no external binaries.
"""

bl_info = {
    "name": "Window Resizer",
    "author": "-",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar (N) > Windows",
    "description": "Set the monitor, pixel size and position of each Blender "
                   "window, and save named layouts that persist between sessions",
    "category": "Interface",
}

import ctypes
import ctypes.util
import itertools
import json
import os
import time
from ctypes import (
    Structure, Union, POINTER, byref,
    c_int, c_uint, c_long, c_ulong, c_ubyte, c_char_p, c_void_p,
)

import bpy
from bpy.app.handlers import persistent


# ---------------------------------------------------------------- tunables
RESTORE_POINTER = True
DRAG_STEPS = 14
STEP_DELAY = 0.012
EDGE_MARGIN = 40            # keep the grab point away from the frame edge
SETTLE_TIMEOUT = 1.2        # max wait for the WM to finish a step
VERBOSE = True

PLACE_ATTEMPTS = 4          # measure-and-correct passes for the final position
POSITION_TOLERANCE = 1      # px; closer than this counts as placed

STARTUP_DELAY = 2.0         # seconds after launch before the startup layout runs
STARTUP_POLL = 0.5          # gap between retries while the WM brings windows up
STARTUP_RETRIES = 24        # ~12 s of patience before giving up
PRESET_FILE = "layouts.json"


def _log(fmt, *args):
    if VERBOSE:
        print("[window-resizer] " + (fmt % args if args else fmt))


class X11Error(RuntimeError):
    pass


# ---------------------------------------------------------------- structs
class XWindowAttributes(Structure):
    _fields_ = [
        ("x", c_int), ("y", c_int),
        ("width", c_int), ("height", c_int),
        ("border_width", c_int),
        ("depth", c_int),
        ("visual", c_void_p),
        ("root", c_ulong),
        ("c_class", c_int),
        ("bit_gravity", c_int),
        ("win_gravity", c_int),
        ("backing_store", c_int),
        ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong),
        ("save_under", c_int),
        ("colormap", c_ulong),
        ("map_installed", c_int),
        ("map_state", c_int),
        ("all_event_masks", c_long),
        ("your_event_mask", c_long),
        ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int),
        ("screen", c_void_p),
    ]


class XClientMessageEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", c_void_p),
        ("window", c_ulong),
        ("message_type", c_ulong),
        ("format", c_int),
        ("data", c_long * 5),
    ]


class XEvent(Union):
    _fields_ = [
        ("type", c_int),
        ("xclient", XClientMessageEvent),
        ("pad", c_long * 24),
    ]


class XRRMonitorInfo(Structure):
    _fields_ = [
        ("name", c_ulong),
        ("primary", c_int),
        ("automatic", c_int),
        ("noutput", c_int),
        ("x", c_int), ("y", c_int),
        ("width", c_int), ("height", c_int),
        ("mwidth", c_int), ("mheight", c_int),
        ("outputs", c_void_p),
    ]


# ---------------------------------------------------------------- libs
_xlib = None
_xtst = None
_xrr = None


def _load_libs():
    global _xlib, _xtst, _xrr
    if _xlib is not None:
        return

    def dl(name, soname):
        path = ctypes.util.find_library(name) or soname
        return ctypes.CDLL(path)

    try:
        xlib = dl("X11", "libX11.so.6")
    except OSError as e:
        raise X11Error("libX11 not loadable: %s" % e)
    try:
        xtst = dl("Xtst", "libXtst.so.6")
    except OSError as e:
        raise X11Error("libXtst not loadable (install libxtst6): %s" % e)
    try:
        xrr = dl("Xrandr", "libXrandr.so.2")
    except OSError:
        xrr = None

    xlib.XOpenDisplay.argtypes = [c_char_p]
    xlib.XOpenDisplay.restype = c_void_p
    xlib.XCloseDisplay.argtypes = [c_void_p]
    xlib.XDefaultRootWindow.argtypes = [c_void_p]
    xlib.XDefaultRootWindow.restype = c_ulong
    xlib.XDefaultScreen.argtypes = [c_void_p]
    xlib.XDefaultScreen.restype = c_int
    xlib.XDisplayWidth.argtypes = [c_void_p, c_int]
    xlib.XDisplayWidth.restype = c_int
    xlib.XDisplayHeight.argtypes = [c_void_p, c_int]
    xlib.XDisplayHeight.restype = c_int
    xlib.XInternAtom.argtypes = [c_void_p, c_char_p, c_int]
    xlib.XInternAtom.restype = c_ulong
    xlib.XGetAtomName.argtypes = [c_void_p, c_ulong]
    xlib.XGetAtomName.restype = c_void_p
    xlib.XFree.argtypes = [c_void_p]
    xlib.XSync.argtypes = [c_void_p, c_int]
    xlib.XFlush.argtypes = [c_void_p]
    xlib.XGetWindowProperty.argtypes = [
        c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
        POINTER(c_ulong), POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong),
        POINTER(POINTER(c_ubyte)),
    ]
    xlib.XGetWindowProperty.restype = c_int
    xlib.XGetWindowAttributes.argtypes = [c_void_p, c_ulong, POINTER(XWindowAttributes)]
    xlib.XGetWindowAttributes.restype = c_int
    xlib.XTranslateCoordinates.argtypes = [
        c_void_p, c_ulong, c_ulong, c_int, c_int,
        POINTER(c_int), POINTER(c_int), POINTER(c_ulong),
    ]
    xlib.XTranslateCoordinates.restype = c_int
    xlib.XQueryTree.argtypes = [
        c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong),
        POINTER(POINTER(c_ulong)), POINTER(c_uint),
    ]
    xlib.XQueryTree.restype = c_int
    xlib.XQueryPointer.argtypes = [
        c_void_p, c_ulong, POINTER(c_ulong), POINTER(c_ulong),
        POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int),
        POINTER(c_uint),
    ]
    xlib.XQueryPointer.restype = c_int
    xlib.XSendEvent.argtypes = [c_void_p, c_ulong, c_int, c_long, POINTER(XEvent)]
    xlib.XSendEvent.restype = c_int
    xlib.XResizeWindow.argtypes = [c_void_p, c_ulong, c_uint, c_uint]
    xlib.XResizeWindow.restype = c_int

    xtst.XTestFakeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_int, c_ulong]
    xtst.XTestFakeMotionEvent.restype = c_int
    xtst.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
    xtst.XTestFakeButtonEvent.restype = c_int

    if xrr is not None:
        xrr.XRRGetMonitors.argtypes = [c_void_p, c_ulong, c_int, POINTER(c_int)]
        xrr.XRRGetMonitors.restype = POINTER(XRRMonitorInfo)
        xrr.XRRFreeMonitors.argtypes = [POINTER(XRRMonitorInfo)]

    _xlib, _xtst, _xrr = xlib, xtst, xrr


def _open_display():
    _load_libs()
    name = os.environ.get("DISPLAY")
    dpy = _xlib.XOpenDisplay(name.encode() if name else None)
    if not dpy:
        if os.environ.get("WAYLAND_DISPLAY"):
            raise X11Error("No X display. On native Wayland window positioning "
                           "is not available; start Blender with "
                           "env -u WAYLAND_DISPLAY blender")
        raise X11Error("Cannot open X display %r" % name)
    return c_void_p(dpy)


def _atom(dpy, name, only_if_exists=False):
    return _xlib.XInternAtom(dpy, name.encode(), 1 if only_if_exists else 0)


def _atom_name(dpy, atom):
    ptr = _xlib.XGetAtomName(dpy, atom)
    if not ptr:
        return ""
    try:
        return ctypes.cast(ptr, c_char_p).value.decode("utf-8", "replace")
    finally:
        _xlib.XFree(ptr)


def _prop32(dpy, win, name):
    a = _atom(dpy, name, True)
    if not a:
        return []
    at, af = c_ulong(), c_int()
    n, ba = c_ulong(), c_ulong()
    data = POINTER(c_ubyte)()
    st = _xlib.XGetWindowProperty(dpy, win, a, 0, 65536, 0, 0,
                                  byref(at), byref(af), byref(n), byref(ba),
                                  byref(data))
    out = []
    if st == 0 and data:
        if af.value == 32 and n.value:
            arr = ctypes.cast(data, POINTER(c_ulong))
            out = [int(arr[i]) for i in range(n.value)]
        _xlib.XFree(data)
    return out


def _frame_extents(dpy, win):
    """
    (left, right, top, bottom) of the VISIBLE decorations, per the WM.
    """
    vals = _prop32(dpy, win, "_NET_FRAME_EXTENTS")
    if len(vals) >= 4:
        return tuple(int(v) for v in vals[:4])
    return (0, 0, 0, 0)


def _invisible_border(dpy, root, win, info=None):
    """
    How far the frame window extends beyond the visible window.

    Mutter and Muffin wrap each window in an invisible grab band, so the frame
    window read from the X tree is bigger than anything on screen.
    _NET_FRAME_EXTENTS reports only the decorations you can see, so the
    difference between the two is the invisible part. Positioning the frame
    origin on a monitor corner therefore lands the visible window inset from
    it, down and to the right.
    """
    if info is None:
        info = _window_info(dpy, root, win)
    vl, vr, vt, vb = _frame_extents(dpy, win)
    il = info["cx"] - info["fx"]
    ir = (info["fx"] + info["fw"]) - (info["cx"] + info["cw"])
    it = info["cy"] - info["fy"]
    ib = (info["fy"] + info["fh"]) - (info["cy"] + info["ch"])
    return (max(0, il - vl), max(0, ir - vr),
            max(0, it - vt), max(0, ib - vb))


def _state_names(dpy, win):
    return [_atom_name(dpy, a) for a in _prop32(dpy, win, "_NET_WM_STATE")]


# ---------------------------------------------------------------- geometry
def _root_size(dpy):
    s = _xlib.XDefaultScreen(dpy)
    return _xlib.XDisplayWidth(dpy, s), _xlib.XDisplayHeight(dpy, s)


def _frame_window(dpy, root, win):
    """
    Walk up to the WM frame (the direct child of root).
    """
    cur = win
    for _ in range(16):
        r, p = c_ulong(), c_ulong()
        ch = POINTER(c_ulong)()
        n = c_uint()
        if not _xlib.XQueryTree(dpy, cur, byref(r), byref(p), byref(ch), byref(n)):
            return cur
        if ch:
            _xlib.XFree(ch)
        if p.value == 0 or p.value == r.value:
            return cur
        cur = p.value
    return cur


def _abs_geom(dpy, root, win):
    attr = XWindowAttributes()
    if not _xlib.XGetWindowAttributes(dpy, win, byref(attr)):
        raise X11Error("XGetWindowAttributes failed for 0x%x" % win)
    x, y = c_int(), c_int()
    child = c_ulong()
    _xlib.XTranslateCoordinates(dpy, win, root, 0, 0, byref(x), byref(y), byref(child))
    return x.value, y.value, attr.width, attr.height


def _window_info(dpy, root, win):
    cx, cy, cw, ch = _abs_geom(dpy, root, win)
    frame = _frame_window(dpy, root, win)
    if frame != win:
        fx, fy, fw, fh = _abs_geom(dpy, root, frame)
    else:
        fx, fy, fw, fh = cx, cy, cw, ch
    return {
        "id": win, "frame": frame,
        "cx": cx, "cy": cy, "cw": cw, "ch": ch,
        "fx": fx, "fy": fy, "fw": fw, "fh": fh,
    }


def _blender_toplevels(dpy, root):
    ids = _prop32(dpy, root, "_NET_CLIENT_LIST")
    if not ids:
        raise X11Error("_NET_CLIENT_LIST missing - unsupported window manager.")
    pid = os.getpid()
    mine = [w for w in ids if (_prop32(dpy, w, "_NET_WM_PID") or [None])[0] == pid]
    if not mine:
        mine = list(ids)
    out = []
    for w in mine:
        try:
            out.append(_window_info(dpy, root, w))
        except X11Error:
            pass
    return out


def _monitors(dpy, root):
    """
    [{name, x, y, w, h}] in xrandr order.
    """
    if _xrr is not None:
        n = c_int()
        ptr = _xrr.XRRGetMonitors(dpy, root, 1, byref(n))
        if ptr and n.value > 0:
            mons = []
            for i in range(n.value):
                mons.append({
                    "name": _atom_name(dpy, ptr[i].name) or "Monitor %d" % i,
                    "x": ptr[i].x, "y": ptr[i].y,
                    "w": ptr[i].width, "h": ptr[i].height,
                    "primary": bool(ptr[i].primary),
                })
            _xrr.XRRFreeMonitors(ptr)
            return mons
    rw, rh = _root_size(dpy)
    return [{"name": "Screen", "x": 0, "y": 0, "w": rw, "h": rh, "primary": True}]


def _pick_monitor(mons, x, y, w, h):
    cx, cy = x + w // 2, y + h // 2
    for m in mons:
        if m["x"] <= cx < m["x"] + m["w"] and m["y"] <= cy < m["y"] + m["h"]:
            return m
    best, best_area = mons[0], -1
    for m in mons:
        ox = max(0, min(x + w, m["x"] + m["w"]) - max(x, m["x"]))
        oy = max(0, min(y + h, m["y"] + m["h"]) - max(y, m["y"]))
        if ox * oy > best_area:
            best, best_area = m, ox * oy
    return best


def _covered(mons, x, y, w, h):
    out = []
    for m in mons:
        if (x < m["x"] + m["w"] and x + w > m["x"] and
                y < m["y"] + m["h"] and y + h > m["y"]):
            out.append(m)
    return out


def _best_corner(mons, mon, fw, fh):
    """
    Corner of `mon` whose neighbourhood the window straddles most widely.

    The window is centered on the corner, so it lands on every monitor touching
    that point. The bottom-right corner is the natural choice, but a monitor on
    the edge of the layout has corners with nothing beyond them, so all four
    are scored and the one covering the most monitors wins.
    """
    best = None
    for name, (cx, cy) in (
            ("bottom-right", (mon["x"] + mon["w"], mon["y"] + mon["h"])),
            ("bottom-left", (mon["x"], mon["y"] + mon["h"])),
            ("top-right", (mon["x"] + mon["w"], mon["y"])),
            ("top-left", (mon["x"], mon["y"]))):
        x, y = cx - fw // 2, cy - fh // 2
        n = len(_covered(mons, x, y, fw, fh))
        if best is None or n > best[0]:
            best = (n, name, (cx, cy))
    return best[1], best[2], best[0]


# ---------------------------------------------------------------- matching
def _match_cost(bw, xw, root_h, conv):
    by = xw["cy"] if conv == "top" else root_h - xw["cy"] - xw["ch"]
    cost = (abs(xw["cw"] - bw.width) + abs(xw["ch"] - bw.height)) * 10
    cost += abs(xw["cx"] - bw.x) + abs(by - bw.y)
    return cost


def _best_assignment(costs, nb, nx):
    """
    Cheapest one-to-one assignment of Blender rows to X11 columns.

    Assigning greedily in row order is wrong: an early row can take a window
    that a later row needed far more badly, and the later row has no way to
    object. Only the total matters, so score whole assignments. Exhaustive
    below eight windows, then a global cheapest-pair-first pass.
    """
    if nb == 0 or nx == 0:
        return {}, 0

    if nb <= 7 and nx <= 8:
        best = None
        for perm in itertools.permutations(range(nx), min(nb, nx)):
            total = sum(costs[i][perm[i]] for i in range(len(perm)))
            if best is None or total < best[0]:
                best = (total, perm)
        total, perm = best
        return {i: perm[i] for i in range(len(perm))}, total

    pairs = sorted((costs[i][j], i, j)
                   for i in range(nb) for j in range(nx))
    mapping, rows, cols, total = {}, set(), set(), 0
    for c, i, j in pairs:
        if i in rows or j in cols:
            continue
        mapping[i], total = j, total + c
        rows.add(i)
        cols.add(j)
    return mapping, total


def _match_windows(bwins, xwins, root_h):
    """
    Map Blender window index -> x11 window info.
    """
    best = None
    for conv in ("top", "bottom"):
        costs = [[_match_cost(bw, xw, root_h, conv) for xw in xwins]
                 for bw in bwins]
        mapping, total = _best_assignment(costs, len(bwins), len(xwins))
        if best is None or total < best[0]:
            best = (total, {i: xwins[j] for i, j in mapping.items()}, conv)
    _log("matched %d window(s) using %s-origin coordinates, total cost %d",
         len(best[1]), best[2], best[0])
    return best[1]


def resolve_windows(context):
    """
    Blender window index -> X11 window id.

    Call this ONCE, before anything is moved. The matching leans on the
    geometry Blender reports for its own windows, and Blender only updates
    that when its event loop runs - which it cannot do while a Set is in
    progress. Re-matching between windows would therefore compare fresh X11
    geometry against Blender's pre-move beliefs and hand back nonsense.
    """
    wins = list(context.window_manager.windows)
    dpy = _open_display()
    try:
        root = _xlib.XDefaultRootWindow(dpy)
        _, root_h = _root_size(dpy)
        xwins = _blender_toplevels(dpy, root)
        if not xwins:
            raise X11Error("No managed Blender windows found on this display.")
        mapping = _match_windows(wins, xwins, root_h)
        for i in sorted(mapping):
            xw, bw = mapping[i], wins[i]
            _log("  window %d: blender says %dx%d at %d,%d -> "
                 "0x%x, x11 says %dx%d at %d,%d",
                 i + 1, bw.width, bw.height, bw.x, bw.y,
                 xw["id"], xw["cw"], xw["ch"], xw["cx"], xw["cy"])
        return {i: xw["id"] for i, xw in mapping.items()}
    finally:
        _xlib.XCloseDisplay(dpy)


# ---------------------------------------------------------------- actions
def _send_root_msg(dpy, root, win, msg, data):
    ev = XEvent()
    ev.xclient.type = 33  # ClientMessage
    ev.xclient.serial = 0
    ev.xclient.send_event = 1
    ev.xclient.display = None
    ev.xclient.window = win
    ev.xclient.message_type = _atom(dpy, msg)
    ev.xclient.format = 32
    for i in range(5):
        ev.xclient.data[i] = int(data[i]) if i < len(data) else 0
    mask = (1 << 19) | (1 << 20)  # SubstructureNotify | SubstructureRedirect
    _xlib.XSendEvent(dpy, root, 0, mask, byref(ev))
    _xlib.XSync(dpy, 0)


def _activate(dpy, root, win):
    _send_root_msg(dpy, root, win, "_NET_ACTIVE_WINDOW", [2, 0, 0, 0, 0])


def _unmaximize(dpy, root, win):
    """
    A maximized window's size belongs to the WM; every resize is discarded.
    """
    horz = _atom(dpy, "_NET_WM_STATE_MAXIMIZED_HORZ")
    vert = _atom(dpy, "_NET_WM_STATE_MAXIMIZED_VERT")
    full = _atom(dpy, "_NET_WM_STATE_FULLSCREEN")
    _send_root_msg(dpy, root, win, "_NET_WM_STATE", [0, horz, vert, 2, 0])
    _send_root_msg(dpy, root, win, "_NET_WM_STATE", [0, full, 0, 2, 0])


def _resize(dpy, win, w, h):
    _xlib.XResizeWindow(dpy, win, int(w), int(h))
    _xlib.XSync(dpy, 0)


def _pointer(dpy, root):
    r, c = c_ulong(), c_ulong()
    rx, ry, wx, wy = c_int(), c_int(), c_int(), c_int()
    m = c_uint()
    _xlib.XQueryPointer(dpy, root, byref(r), byref(c),
                        byref(rx), byref(ry), byref(wx), byref(wy), byref(m))
    return rx.value, ry.value


def _toplevel_under_pointer(dpy, root):
    """
    The direct child of root under the pointer, i.e. the frame it would hit.
    """
    r, c = c_ulong(), c_ulong()
    rx, ry, wx, wy = c_int(), c_int(), c_int(), c_int()
    m = c_uint()
    if not _xlib.XQueryPointer(dpy, root, byref(r), byref(c),
                               byref(rx), byref(ry), byref(wx), byref(wy),
                               byref(m)):
        return 0
    return c.value


def _motion(dpy, screen, x, y):
    _xtst.XTestFakeMotionEvent(dpy, screen, int(x), int(y), 0)
    _xlib.XSync(dpy, 0)


def _button(dpy, pressed):
    _xtst.XTestFakeButtonEvent(dpy, 1, 1 if pressed else 0, 0)
    _xlib.XSync(dpy, 0)


def _drag(dpy, screen, sx, sy, ex, ey):
    _motion(dpy, screen, sx, sy)
    time.sleep(0.06)
    _button(dpy, True)
    time.sleep(0.10)
    # exceed the WM's drag threshold before the real travel
    _motion(dpy, screen, sx + 8, sy + 8)
    time.sleep(0.03)
    for i in range(1, DRAG_STEPS + 1):
        t = i / float(DRAG_STEPS)
        _motion(dpy, screen, round(sx + (ex - sx) * t), round(sy + (ey - sy) * t))
        time.sleep(STEP_DELAY)
    _motion(dpy, screen, ex, ey)
    time.sleep(0.06)
    _button(dpy, False)
    _xlib.XSync(dpy, 0)


def _settled(dpy, root, win, timeout=SETTLE_TIMEOUT):
    """
    Wait until the geometry stops changing, then return it.
    """
    deadline = time.time() + timeout
    last, stable = None, 0
    while time.time() < deadline:
        info = _window_info(dpy, root, win)
        now = (info["fx"], info["fy"], info["fw"], info["fh"])
        if now == last:
            stable += 1
            if stable >= 2:
                return info
        else:
            stable, last = 0, now
        time.sleep(0.04)
    return _window_info(dpy, root, win)


def _grab_offsets(info):
    """
    Where to press, relative to the frame origin, to grab the titlebar.
    """
    top = max(0, info["cy"] - info["fy"])
    gx = max(12, info["fw"] // 2 - EDGE_MARGIN)
    undecorated = top < 6
    gy = min(20, max(2, info["fh"] // 2)) if undecorated else max(2, top // 2)
    return gx, gy, undecorated


def _drag_frame_to(dpy, screen, root, win, tx, ty, root_w, root_h):
    """
    Drag the window so its frame's top-left corner lands on (tx, ty).
    """
    info = _window_info(dpy, root, win)
    gx, gy, undecorated = _grab_offsets(info)
    sx, sy = info["fx"] + gx, info["fy"] + gy
    ex, ey = tx + gx, ty + gy
    ex = max(1, min(root_w - 2, ex))
    ey = max(1, min(root_h - 2, ey))
    _log("  drag (%d,%d) -> (%d,%d)%s", sx, sy, ex, ey,
         "  [no titlebar: interactive move]" if undecorated else "")

    # A synthetic press goes to whatever top-level sits under the pointer, not
    # to the window we mean. If something else is stacked over the grab point,
    # raise ours and look again before committing to the press.
    _motion(dpy, screen, sx, sy)
    time.sleep(0.04)
    hit = _toplevel_under_pointer(dpy, root)
    if hit and hit != info["frame"] and hit != win:
        _log("  grab point is covered by 0x%x, raising 0x%x", hit, win)
        _activate(dpy, root, win)
        time.sleep(0.12)
        _motion(dpy, screen, sx, sy)
        time.sleep(0.04)
        hit = _toplevel_under_pointer(dpy, root)
        if hit and hit != info["frame"] and hit != win:
            raise X11Error("grab point %d,%d belongs to window 0x%x, not 0x%x"
                           % (sx, sy, hit, win))

    if undecorated:
        _motion(dpy, screen, sx, sy)
        time.sleep(0.05)
        _button(dpy, True)
        time.sleep(0.05)
        _send_root_msg(dpy, root, win, "_NET_WM_MOVERESIZE", [sx, sy, 8, 1, 2])
        time.sleep(0.05)
        for i in range(1, DRAG_STEPS + 1):
            t = i / float(DRAG_STEPS)
            _motion(dpy, screen,
                    round(sx + (ex - sx) * t), round(sy + (ey - sy) * t))
            time.sleep(STEP_DELAY)
        _motion(dpy, screen, ex, ey)
        time.sleep(0.06)
        _button(dpy, False)
        _xlib.XSync(dpy, 0)
    else:
        _drag(dpy, screen, sx, sy, ex, ey)


# ---------------------------------------------------------------- the job
def _visible_origin(dpy, root, win, info=None):
    """
    Top-left of the window as the user sees it, ignoring invisible borders.
    """
    if info is None:
        info = _window_info(dpy, root, win)
    inv_l, _, inv_t, _ = _invisible_border(dpy, root, win, info)
    return info["fx"] + inv_l, info["fy"] + inv_t


def _place_visible_at(dpy, screen, root, win, vtx, vty, root_w, root_h):
    """
    Drag until the VISIBLE top-left corner sits on (vtx, vty).

    A single drag was enough while the only target was a monitor corner, where
    window managers snap in the direction you already wanted. An arbitrary
    coordinate has no such help: edge resistance, snapping to other windows and
    pointer clamping all leave a residual. So measure and correct, rather than
    dragging once and hoping. Returns (info, (dx, dy)) with the leftover error.
    """
    for n in range(PLACE_ATTEMPTS):
        info = _window_info(dpy, root, win)
        vx, vy = _visible_origin(dpy, root, win, info)
        dx, dy = vtx - vx, vty - vy
        if abs(dx) <= POSITION_TOLERANCE and abs(dy) <= POSITION_TOLERANCE:
            break
        if n:
            _log("  pass %d: still %+d,%+d out, correcting", n + 1, dx, dy)
        _drag_frame_to(dpy, screen, root, win,
                       info["fx"] + dx, info["fy"] + dy, root_w, root_h)
        _settled(dpy, root, win)

    info = _window_info(dpy, root, win)
    vx, vy = _visible_origin(dpy, root, win, info)
    info["vx"], info["vy"] = vx, vy
    return info, (vtx - vx, vty - vy)


def set_window_size(context, index, mon_name, width, height, xid=None,
                    pos_mode='MONITOR', pos_x=0, pos_y=0):
    wins = list(context.window_manager.windows)
    if index >= len(wins):
        raise X11Error("Window %d no longer exists" % (index + 1))

    dpy = _open_display()
    try:
        root = _xlib.XDefaultRootWindow(dpy)
        screen = _xlib.XDefaultScreen(dpy)
        root_w, root_h = _root_size(dpy)

        if xid is not None:
            if xid not in _prop32(dpy, root, "_NET_CLIENT_LIST"):
                raise X11Error("Window %d closed while it was being placed"
                               % (index + 1))
            win = xid
        else:
            xwins = _blender_toplevels(dpy, root)
            if not xwins:
                raise X11Error("No managed Blender windows found on this display.")
            xw = _match_windows(wins, xwins, root_h).get(index)
            if xw is None:
                raise X11Error("Could not match Blender window %d to an X11 "
                               "window." % (index + 1))
            win = xw["id"]
        _log("target x11 window 0x%x", win)

        _activate(dpy, root, win)
        time.sleep(0.08)

        states = _state_names(dpy, win)
        if any("MAXIMIZED" in s or "FULLSCREEN" in s for s in states):
            _log("un-maximizing (state was %s)", ", ".join(states))
            _unmaximize(dpy, root, win)
            _settled(dpy, root, win)

        info = _window_info(dpy, root, win)
        mons = _monitors(dpy, root)
        mon = next((m for m in mons if m["name"] == mon_name), None)
        if mon is None:
            mon = _pick_monitor(mons, info["fx"], info["fy"],
                                info["fw"], info["fh"])
            _log("no monitor set, using %s", mon["name"])

        if pos_mode == 'ABSOLUTE':
            vtx, vty = int(pos_x), int(pos_y)
        elif pos_mode == 'OFFSET':
            vtx, vty = mon["x"] + int(pos_x), mon["y"] + int(pos_y)
        else:
            vtx, vty = mon["x"], mon["y"]
        _log("target visible top-left %d,%d (%s, monitor %s at %d,%d)",
             vtx, vty, pos_mode, mon["name"], mon["x"], mon["y"])

        old_pointer = _pointer(dpy, root)
        try:
            # --- step 1: centre on a corner of the target monitor
            corner_name, (ccx, ccy), n_cover = _best_corner(
                mons, mon, info["fw"], info["fh"])
            tx, ty = ccx - info["fw"] // 2, ccy - info["fh"] // 2
            _log("step 1: centre on %s %s corner (%d,%d); "
                 "frame %dx%d -> top-left %d,%d",
                 mon["name"], corner_name, ccx, ccy,
                 info["fw"], info["fh"], tx, ty)
            _drag_frame_to(dpy, screen, root, win, tx, ty, root_w, root_h)

            # --- step 2: let the move register
            info = _settled(dpy, root, win)
            cover = _covered(mons, info["fx"], info["fy"], info["fw"], info["fh"])
            _log("step 2: frame at %d,%d %dx%d, on %s",
                 info["fx"], info["fy"], info["fw"], info["fh"],
                 ", ".join(m["name"] for m in cover) or "nothing")
            if len(cover) < 2:
                _log("  warning: only one monitor; the resize will be clamped")

            # --- step 3: resize
            _log("step 3: resize client to %dx%d", width, height)
            _resize(dpy, win, width, height)
            info = _settled(dpy, root, win)
            _log("  client now %dx%d, frame %dx%d",
                 info["cw"], info["ch"], info["fw"], info["fh"])

            # --- step 4: place the VISIBLE corner on the requested point
            _log("step 4: place visible top-left at %d,%d", vtx, vty)
            info, (dx, dy) = _place_visible_at(dpy, screen, root, win,
                                               vtx, vty, root_w, root_h)
            info["want_x"], info["want_y"] = vtx, vty
            info["dx"], info["dy"] = dx, dy
            if dx or dy:
                _log("  settled %+d,%+d from the request; the window manager "
                     "may be snapping, or the grab point cannot reach", dx, dy)
        finally:
            if RESTORE_POINTER:
                _motion(dpy, screen, old_pointer[0], old_pointer[1])

        info.setdefault("vx", info["fx"])
        info.setdefault("vy", info["fy"])
        _log("final: client %dx%d at %d,%d, frame %dx%d at %d,%d, "
             "visible top-left %d,%d",
             info["cw"], info["ch"], info["cx"], info["cy"],
             info["fw"], info["fh"], info["fx"], info["fy"],
             info["vx"], info["vy"])
        return info, mon
    finally:
        _xlib.XCloseDisplay(dpy)


def probe_window(context, index):
    """
    Where a Blender window actually is right now, straight from X11.

    Blender's own width/height exclude the frame and its x/y use a different
    origin, so read the server instead of trusting the Python-side values.
    """
    ids = resolve_windows(context)
    if index not in ids:
        raise X11Error("Could not match Blender window %d to an X11 window."
                       % (index + 1))
    dpy = _open_display()
    try:
        root = _xlib.XDefaultRootWindow(dpy)
        win = ids[index]
        info = _window_info(dpy, root, win)
        vx, vy = _visible_origin(dpy, root, win, info)
        mons = _monitors(dpy, root)
        mon = _pick_monitor(mons, info["fx"], info["fy"],
                            info["fw"], info["fh"])
        return {"vx": vx, "vy": vy, "cw": info["cw"], "ch": info["ch"],
                "monitor": mon["name"], "mx": mon["x"], "my": mon["y"]}
    finally:
        _xlib.XCloseDisplay(dpy)


def list_monitors():
    dpy = _open_display()
    try:
        return _monitors(dpy, _xlib.XDefaultRootWindow(dpy))
    finally:
        _xlib.XCloseDisplay(dpy)


# ---------------------------------------------------------------- storage
#
# Layouts live in a JSON file under Blender's CONFIG directory rather than in
# AddonPreferences, so they work whether the file is installed as an add-on or
# just run from the Text Editor, and so they do not depend on the user
# remembering to save preferences.
#
#   {"startup": "Dual 4K",
#    "layouts": {"Dual 4K": {"windows": [{"monitor": "DP-2",
#                                         "width": 3840, "height": 2160,
#                                         "use": true}, ...]}}}

def _config_dir():
    d = ""
    try:
        d = bpy.utils.user_resource('CONFIG', path="window_resizer", create=True)
    except TypeError:                       # older signature
        base = bpy.utils.user_resource('CONFIG')
        d = os.path.join(base, "window_resizer") if base else ""
    if not d:
        d = os.path.join(os.path.expanduser("~"), ".config", "window_resizer")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _store_path():
    return os.path.join(_config_dir(), PRESET_FILE)


# text cache keyed on mtime+size, so draw() can read the store every redraw
_store_cache = {"stamp": None, "text": "{}"}


def _blank_store():
    return {"startup": "", "layouts": {}}


def _read_store():
    """
    Return a fresh, sanitised copy of the store. Never raises.
    """
    path = _store_path()
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None

    if stamp is None:
        _store_cache["stamp"], _store_cache["text"] = None, "{}"
    elif stamp != _store_cache["stamp"]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _store_cache["text"] = fh.read()
        except OSError as e:
            _log("cannot read %s: %s", path, e)
            _store_cache["text"] = "{}"
        _store_cache["stamp"] = stamp

    try:
        data = json.loads(_store_cache["text"])
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}

    out = _blank_store()
    layouts = data.get("layouts")
    if isinstance(layouts, dict):
        for name, entry in layouts.items():
            wins = entry.get("windows") if isinstance(entry, dict) else entry
            if isinstance(wins, list):
                out["layouts"][str(name)] = {"windows": wins}
    startup = data.get("startup")
    if isinstance(startup, str) and startup in out["layouts"]:
        out["startup"] = startup
    return out


def _write_store(data):
    """
    Atomic write. Raises OSError on failure so callers can report it.
    """
    path = _store_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    _store_cache["stamp"] = None            # force a re-read
    _log("layouts saved to %s", path)


POS_MODES = ('MONITOR', 'OFFSET', 'ABSOLUTE')


def _slots_to_entries(wm):
    return [{"monitor": s.monitor,
             "width": int(s.width),
             "height": int(s.height),
             "use": bool(s.use),
             "pos_mode": s.pos_mode,
             "pos_x": int(s.pos_x),
             "pos_y": int(s.pos_y)}
            for s in wm.window_resizer_slots]


def _entries_to_slots(wm, entries):
    """
    Copy a saved layout into the live slots. Returns how many were filled.

    Layouts written before positioning existed have no pos_* keys; they fall
    back to the monitor corner, which is what they meant.
    """
    slots = wm.window_resizer_slots
    n = 0
    for i, e in enumerate(entries):
        if i >= len(slots):
            break
        if not isinstance(e, dict):
            continue
        slot = slots[i]
        try:
            mon = str(e.get("monitor", ""))
            w = max(64, min(32767, int(e.get("width", slot.width))))
            h = max(64, min(32767, int(e.get("height", slot.height))))
            use = bool(e.get("use", True))
            mode = str(e.get("pos_mode", 'MONITOR'))
            if mode not in POS_MODES:
                mode = 'MONITOR'
            px = max(-32768, min(32767, int(e.get("pos_x", 0))))
            py = max(-32768, min(32767, int(e.get("pos_y", 0))))
        except (TypeError, ValueError):
            continue                        # leave this slot as it was
        slot.monitor, slot.width, slot.height, slot.use = mon, w, h, use
        slot.pos_mode, slot.pos_x, slot.pos_y = mode, px, py
        n += 1
    return n


# Blender does not copy the strings returned by a dynamic enum callback, so the
# list has to outlive the call.
_enum_cache = []


def _preset_items(self, context):
    _enum_cache.clear()
    store = _read_store()
    for i, name in enumerate(sorted(store["layouts"], key=str.lower)):
        n = len(store["layouts"][name].get("windows", []))
        tag = " \u2605" if name == store["startup"] else ""
        _enum_cache.append(
            (name, name + tag, "%d window%s" % (n, "" if n == 1 else "s"), i))
    return _enum_cache


def _current_preset(wm):
    try:
        return wm.window_resizer_preset or ""
    except (AttributeError, TypeError):
        return ""


# ---------------------------------------------------------------- data
class WRZ_Monitor(bpy.types.PropertyGroup):
    x: bpy.props.IntProperty()
    y: bpy.props.IntProperty()
    w: bpy.props.IntProperty()
    h: bpy.props.IntProperty()


class WRZ_Slot(bpy.types.PropertyGroup):
    width: bpy.props.IntProperty(name="Width", default=1920, min=64, max=32767,
                                 subtype='PIXEL')
    height: bpy.props.IntProperty(name="Height", default=1080, min=64, max=32767,
                                  subtype='PIXEL')
    use: bpy.props.BoolProperty(name="Include", default=True,
                                description="Include this window in Set All")
    monitor: bpy.props.StringProperty(
        name="Monitor",
        description="Monitor to place this window on. "
                    "Empty means the monitor it is already on")
    pos_mode: bpy.props.EnumProperty(
        name="Position",
        description="Where the visible top-left corner should end up",
        items=[
            ('MONITOR', "Monitor Corner",
             "Top-left corner of the chosen monitor"),
            ('OFFSET', "Offset from Monitor",
             "X and Y measured from the chosen monitor's top-left corner"),
            ('ABSOLUTE', "Desktop Coordinates",
             "X and Y measured from the top-left of the whole desktop"),
        ],
        default='MONITOR')
    pos_x: bpy.props.IntProperty(name="X", default=0, min=-32768, max=32767,
                                 subtype='PIXEL')
    pos_y: bpy.props.IntProperty(name="Y", default=0, min=-32768, max=32767,
                                 subtype='PIXEL')


def _refresh_monitors(wm):
    coll = wm.window_resizer_monitors
    coll.clear()
    for m in list_monitors():
        item = coll.add()
        item.name = m["name"]
        item.x, item.y, item.w, item.h = m["x"], m["y"], m["w"], m["h"]
    return len(coll)


def _redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            area.tag_redraw()


def _sync():
    try:
        wm = bpy.context.window_manager
        slots = getattr(wm, "window_resizer_slots", None)
        if slots is None:
            return 1.0
        changed = False
        while len(slots) < len(wm.windows):
            slot = slots.add()
            win = wm.windows[len(slots) - 1]
            slot.width, slot.height = max(win.width, 64), max(win.height, 64)
            changed = True
        while len(slots) > len(wm.windows):
            slots.remove(len(slots) - 1)
            changed = True
        if not len(wm.window_resizer_monitors):
            _refresh_monitors(wm)
            changed = True
        if changed:
            _redraw()
    except Exception:
        pass
    return 1.0


# ---------------------------------------------------------------- startup
def _apply_entries(wm, entries, label):
    """
    Run set_window_size for every entry flagged `use`. Returns (done, fails).
    """
    done, fails = 0, 0
    count = min(len(entries), len(wm.window_resizer_slots), len(wm.windows))
    try:
        ids = resolve_windows(bpy.context)
    except Exception as e:  # noqa: BLE001
        _log("%s: cannot identify the windows: %s", label, e)
        return 0, count

    for i in range(count):
        slot = wm.window_resizer_slots[i]
        if not slot.use:
            continue
        _log("--- %s: window %d -> %s, %dx%d, %s %d,%d ---", label, i + 1,
             slot.monitor or "(current monitor)", slot.width, slot.height,
             slot.pos_mode, slot.pos_x, slot.pos_y)
        if i not in ids:
            fails += 1
            _log("  window %d has no matching X11 window", i + 1)
            continue
        try:
            set_window_size(bpy.context, i, slot.monitor, slot.width,
                            slot.height, xid=ids[i], pos_mode=slot.pos_mode,
                            pos_x=slot.pos_x, pos_y=slot.pos_y)
            done += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            _log("  window %d failed: %s: %s", i + 1, type(e).__name__, e)
    return done, fails


_startup_state = {"scheduled": False, "done": False, "tries": 0}


def _x11_window_count():
    """
    How many Blender top-levels the window manager has published, or -1.
    """
    try:
        dpy = _open_display()
    except X11Error:
        return -1
    try:
        root = _xlib.XDefaultRootWindow(dpy)
        return len(_blender_toplevels(dpy, root))
    except X11Error:
        return -1
    finally:
        _xlib.XCloseDisplay(dpy)


def _run_startup_layout(force=False):
    """
    Apply the layout flagged as startup. Returns a human-readable result.

    Raises nothing. `force` ignores the once-per-session guard so the button in
    the Options panel can exercise exactly this path on demand.
    """
    store = _read_store()
    name = store["startup"]
    if not name:
        return "No layout is flagged for startup"
    entries = store["layouts"].get(name, {}).get("windows")
    if not entries:
        return "Startup layout %r has no windows saved" % name

    wm = getattr(bpy.context, "window_manager", None)
    if wm is None or not len(wm.windows):
        return "Blender has no windows yet"

    # The X11 window manager publishes a window in _NET_CLIENT_LIST some time
    # after Blender creates it. Acting before that leaves nothing to match
    # against, so treat a short list as "not ready" rather than as a failure.
    nx = _x11_window_count()
    if nx < len(wm.windows):
        return ("Window manager has published %d of %d window(s)"
                % (max(nx, 0), len(wm.windows)))

    if _startup_state["done"] and not force:
        return "Startup layout already applied this session"
    _startup_state["done"] = True

    _sync()                                 # make sure the slots exist
    filled = _entries_to_slots(wm, entries)
    if len(entries) > len(wm.windows):
        _log("startup layout %r holds %d window(s), Blender has %d - "
             "applying the first %d",
             name, len(entries), len(wm.windows), filled)
    done, fails = _apply_entries(wm, entries, "startup")
    _redraw()
    return ("Applied %r: %d placed, %d failed" % (name, done, fails)
            if fails else "Applied %r to %d window(s)" % (name, done))


def _startup_apply():
    """
    Timer callback. Retries while the WM is still bringing windows up.
    """
    _startup_state["tries"] += 1
    try:
        msg = _run_startup_layout()
    except Exception as e:  # noqa: BLE001
        _log("startup layout failed: %s: %s", type(e).__name__, e)
        _startup_state["scheduled"] = False
        return None

    retryable = msg.startswith(("Blender has no windows",
                                "Window manager has published"))
    if retryable and _startup_state["tries"] <= STARTUP_RETRIES:
        _log("startup layout: %s, retrying (%d/%d)",
             msg, _startup_state["tries"], STARTUP_RETRIES)
        return STARTUP_POLL

    _log("startup layout: %s%s", msg,
         " - gave up after %d tries" % (STARTUP_RETRIES,) if retryable else "")
    _startup_state["scheduled"] = False
    return None


def _schedule_startup(delay):
    if _startup_state["done"] or _startup_state["scheduled"]:
        return
    _startup_state["scheduled"] = True
    _startup_state["tries"] = 0
    if not bpy.app.timers.is_registered(_startup_apply):
        bpy.app.timers.register(_startup_apply,
                                first_interval=delay, persistent=True)


@persistent
def _on_load_post(_dummy):
    """
    Blender fires this for the startup file too, which is the launch we want.

    The once-per-session guard in _run_startup_layout keeps a later File > Open
    from rearranging the user's windows underneath them.
    """
    if not bpy.app.background:
        _schedule_startup(STARTUP_DELAY)


def _launching():
    """
    True when register() is running as part of Blender's own startup.

    Add-ons are registered before the startup file's windows exist, so an empty
    window list is a reliable-enough signal that this is a launch rather than
    someone ticking the checkbox in Preferences mid-session. This only exists
    as a backstop for the load_post handler: whichever fires first wins, and
    _schedule_startup makes the second one a no-op.
    """
    if bpy.app.background:
        return False
    try:
        wm = bpy.context.window_manager
        return wm is None or len(wm.windows) == 0
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- operators
class WRZ_OT_refresh(bpy.types.Operator):
    bl_idname = "wm.window_resizer_refresh"
    bl_label = "Refresh Monitors"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            n = _refresh_monitors(context.window_manager)
        except X11Error as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, "Found %d monitor(s)" % n)
        _redraw()
        return {'FINISHED'}


class WRZ_OT_grab(bpy.types.Operator):
    bl_idname = "wm.window_resizer_grab"
    bl_label = "Grab"
    bl_description = ("Fill the fields from this window's current monitor, "
                      "size and position")
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty(default=0, min=0)

    def execute(self, context):
        wm = context.window_manager
        slots = wm.window_resizer_slots
        if self.index >= min(len(slots), len(wm.windows)):
            return {'CANCELLED'}
        slot = slots[self.index]

        try:
            g = probe_window(context, self.index)
        except Exception as e:  # noqa: BLE001
            # Fall back to Blender's numbers: size only, no position.
            win = wm.windows[self.index]
            slot.width = max(win.width, 64)
            slot.height = max(win.height, 64)
            _redraw()
            self.report({'WARNING'},
                        "Grabbed the size only (%s: %s)" % (type(e).__name__, e))
            return {'FINISHED'}

        slot.width = max(64, min(32767, g["cw"]))
        slot.height = max(64, min(32767, g["ch"]))
        slot.monitor = g["monitor"]
        # Fill the offset even in Monitor Corner mode, so switching modes
        # starts from where the window already is instead of from 0,0.
        slot.pos_x = (g["vx"] if slot.pos_mode == 'ABSOLUTE'
                      else g["vx"] - g["mx"])
        slot.pos_y = (g["vy"] if slot.pos_mode == 'ABSOLUTE'
                      else g["vy"] - g["my"])
        _redraw()
        self.report({'INFO'}, "%dx%d at %d,%d on %s"
                    % (g["cw"], g["ch"], g["vx"], g["vy"], g["monitor"]))
        return {'FINISHED'}


class WRZ_OT_set(bpy.types.Operator):
    bl_idname = "wm.window_resizer_set"
    bl_label = "Set"
    bl_description = ("Move the window onto a monitor corner, resize it, then "
                      "place its top-left corner where the fields ask for")
    bl_options = {'REGISTER'}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        wm = context.window_manager
        slots = wm.window_resizer_slots
        count = min(len(slots), len(wm.windows))
        targets = ([i for i in range(count) if slots[i].use]
                   if self.index < 0 else
                   [self.index] if self.index < count else [])

        # Resolve every window up front: Blender cannot update the geometry
        # this depends on while the operator is running.
        ids = {}
        if targets:
            try:
                ids = resolve_windows(context)
            except X11Error as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}

        done, problems = 0, []
        for i in targets:
            slot = slots[i]
            _log("--- window %d -> %s, %dx%d, %s %d,%d ---", i + 1,
                 slot.monitor or "(current monitor)", slot.width, slot.height,
                 slot.pos_mode, slot.pos_x, slot.pos_y)
            if i not in ids:
                problems.append("window %d: no matching X11 window" % (i + 1))
                continue
            try:
                info, mon = set_window_size(context, i, slot.monitor,
                                            slot.width, slot.height,
                                            xid=ids[i],
                                            pos_mode=slot.pos_mode,
                                            pos_x=slot.pos_x,
                                            pos_y=slot.pos_y)
            except X11Error as e:
                problems.append("window %d: %s" % (i + 1, e))
                continue
            except Exception as e:  # noqa: BLE001
                problems.append("window %d: %s: %s" % (i + 1, type(e).__name__, e))
                continue

            if (info["cw"], info["ch"]) != (slot.width, slot.height):
                problems.append("window %d: size came back %dx%d"
                                % (i + 1, info["cw"], info["ch"]))
            elif info["dx"] or info["dy"]:
                problems.append("window %d: corner at %d,%d not %d,%d"
                                % (i + 1, info["vx"], info["vy"],
                                   info["want_x"], info["want_y"]))
            else:
                done += 1

        _redraw()
        if problems:
            self.report({'WARNING'}, "Placed %d. %s" % (done, "; ".join(problems)))
        elif done:
            self.report({'INFO'}, "Placed %d window%s"
                        % (done, "" if done == 1 else "s"))
        else:
            self.report({'WARNING'}, "Nothing to do")
        return {'FINISHED'}


# ---------------------------------------------------------------- presets UI ops
class WRZ_OT_preset_save(bpy.types.Operator):
    bl_idname = "wm.window_resizer_preset_save"
    bl_label = "Save Layout"
    bl_description = ("Store the monitor, size and include flag of every window "
                      "under a name that survives restarts")
    bl_options = {'INTERNAL'}

    name: bpy.props.StringProperty(
        name="Name", description="Name to save this layout under")

    def invoke(self, context, event):
        wm = context.window_manager
        self.name = _current_preset(wm) or "Layout"
        return wm.invoke_props_dialog(self, width=300)

    def draw(self, context):
        self.layout.prop(self, "name")
        existing = _read_store()["layouts"]
        if self.name.strip() in existing:
            self.layout.label(text="Overwrites the existing layout",
                              icon='ERROR')

    def execute(self, context):
        wm = context.window_manager
        name = self.name.strip()
        if not name:
            self.report({'ERROR'}, "Give the layout a name")
            return {'CANCELLED'}

        entries = _slots_to_entries(wm)
        if not entries:
            self.report({'ERROR'}, "No windows tracked yet")
            return {'CANCELLED'}

        store = _read_store()
        overwrote = name in store["layouts"]
        store["layouts"][name] = {"windows": entries}
        try:
            _write_store(store)
        except OSError as e:
            self.report({'ERROR'}, "Could not write %s: %s" % (_store_path(), e))
            return {'CANCELLED'}

        wm.window_resizer_preset = name
        _redraw()
        self.report({'INFO'}, "%s %r (%d window%s)"
                    % ("Replaced" if overwrote else "Saved", name,
                       len(entries), "" if len(entries) == 1 else "s"))
        return {'FINISHED'}


class WRZ_OT_preset_load(bpy.types.Operator):
    bl_idname = "wm.window_resizer_preset_load"
    bl_label = "Load"
    bl_description = ("Copy the saved layout into the fields above without "
                      "moving anything")
    bl_options = {'INTERNAL'}

    apply: bpy.props.BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        wm = context.window_manager
        name = _current_preset(wm)
        store = _read_store()
        entry = store["layouts"].get(name)
        if entry is None:
            self.report({'ERROR'}, "No layout selected")
            return {'CANCELLED'}

        entries = entry.get("windows", [])
        filled = _entries_to_slots(wm, entries)
        _redraw()

        if filled < len(entries):
            self.report({'WARNING'},
                        "Loaded %d of %d saved window(s) - the rest have no "
                        "open window to go with them" % (filled, len(entries)))
        elif not self.apply:
            self.report({'INFO'}, "Loaded %r" % name)

        if self.apply and filled:
            return bpy.ops.wm.window_resizer_set(index=-1)
        return {'FINISHED'}


class WRZ_OT_preset_delete(bpy.types.Operator):
    bl_idname = "wm.window_resizer_preset_delete"
    bl_label = "Delete Layout"
    bl_description = "Remove the selected layout from disk"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        wm = context.window_manager
        name = _current_preset(wm)
        store = _read_store()
        if name not in store["layouts"]:
            self.report({'ERROR'}, "No layout selected")
            return {'CANCELLED'}
        del store["layouts"][name]
        if store["startup"] == name:
            store["startup"] = ""
        try:
            _write_store(store)
        except OSError as e:
            self.report({'ERROR'}, "Could not write %s: %s" % (_store_path(), e))
            return {'CANCELLED'}
        _redraw()
        self.report({'INFO'}, "Deleted %r" % name)
        return {'FINISHED'}


class WRZ_OT_preset_startup(bpy.types.Operator):
    bl_idname = "wm.window_resizer_preset_startup"
    bl_label = "Apply on Startup"
    bl_description = ("Apply this layout automatically a couple of seconds "
                      "after Blender launches")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        wm = context.window_manager
        name = _current_preset(wm)
        store = _read_store()
        if name not in store["layouts"]:
            self.report({'ERROR'}, "No layout selected")
            return {'CANCELLED'}
        store["startup"] = "" if store["startup"] == name else name
        try:
            _write_store(store)
        except OSError as e:
            self.report({'ERROR'}, "Could not write %s: %s" % (_store_path(), e))
            return {'CANCELLED'}
        _redraw()
        self.report({'INFO'}, "Startup layout: %s" % (store["startup"] or "none"))
        return {'FINISHED'}


class WRZ_OT_startup_now(bpy.types.Operator):
    bl_idname = "wm.window_resizer_startup_now"
    bl_label = "Run Startup Layout Now"
    bl_description = ("Run exactly what the startup timer runs, and report what "
                      "it found. Useful for checking why nothing happened at launch")
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            msg = _run_startup_layout(force=True)
        except Exception as e:  # noqa: BLE001
            self.report({'ERROR'}, "%s: %s" % (type(e).__name__, e))
            return {'CANCELLED'}
        self.report({'INFO'} if msg.startswith("Applied") else {'WARNING'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------- UI
class WRZ_PT_panel(bpy.types.Panel):
    bl_label = "Window Sizes"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Windows"

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        slots = wm.window_resizer_slots
        count = min(len(slots), len(wm.windows))
        if count == 0:
            layout.label(text="No windows tracked yet", icon='INFO')
            return

        for i in range(count):
            slot, win = slots[i], wm.windows[i]
            box = layout.box()
            head = box.row(align=True)
            head.prop(slot, "use", text="")
            head.label(text="Window %d%s"
                       % (i + 1, "  (this one)" if win == context.window else ""))
            head.label(text="%d \u00d7 %d" % (win.width, win.height))

            box.prop_search(slot, "monitor", wm, "window_resizer_monitors",
                            text="", icon='DESKTOP')
            col = box.column(align=True)
            col.prop(slot, "width")
            col.prop(slot, "height")

            box.prop(slot, "pos_mode", text="")
            if slot.pos_mode != 'MONITOR':
                col = box.column(align=True)
                col.prop(slot, "pos_x")
                col.prop(slot, "pos_y")
                if slot.pos_mode == 'OFFSET':
                    mon = wm.window_resizer_monitors.get(slot.monitor)
                    if mon is not None:
                        box.label(text="Desktop: %d, %d"
                                       % (mon.x + slot.pos_x,
                                          mon.y + slot.pos_y),
                                  icon='ORIENTATION_GLOBAL')
                    else:
                        box.label(text="Relative to whichever monitor it is on",
                                  icon='INFO')

            row = box.row(align=True)
            row.operator("wm.window_resizer_grab", text="Grab",
                         icon='EYEDROPPER').index = i
            row.operator("wm.window_resizer_set", text="Set",
                         icon='CHECKMARK').index = i

        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("wm.window_resizer_set", text="Set All Windows",
                     icon='FULLSCREEN_ENTER').index = -1


class WRZ_PT_presets(bpy.types.Panel):
    bl_label = "Saved Layouts"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Windows"
    bl_parent_id = "WRZ_PT_panel"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        store = _read_store()
        name = _current_preset(wm)
        have = bool(store["layouts"])

        row = layout.row(align=True)
        if have:
            row.prop(wm, "window_resizer_preset", text="")
        else:
            row.label(text="None saved yet", icon='INFO')
        row.operator("wm.window_resizer_preset_save", text="", icon='ADD')
        sub = row.row(align=True)
        sub.enabled = have
        sub.operator("wm.window_resizer_preset_delete", text="", icon='REMOVE')

        col = layout.column(align=True)
        col.enabled = have and bool(name)

        row = col.row(align=True)
        row.operator("wm.window_resizer_preset_load",
                     text="Load Fields", icon='IMPORT').apply = False
        op = row.operator("wm.window_resizer_preset_load",
                          text="Load & Set", icon='PLAY')
        op.apply = True

        col.operator("wm.window_resizer_preset_startup",
                     text="Apply on Startup", icon='FILE_TICK',
                     depress=bool(name and store["startup"] == name))

        if store["startup"]:
            layout.label(text="On startup: %s" % store["startup"], icon='SOLO_ON')


class WRZ_PT_options(bpy.types.Panel):
    bl_label = "Options"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Windows"
    bl_parent_id = "WRZ_PT_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        layout.prop(wm, "window_resizer_verbose")

        store = _read_store()
        col = layout.column(align=True)
        col.enabled = bool(store["startup"])
        col.operator("wm.window_resizer_startup_now", icon='PLAY')
        if not store["startup"]:
            layout.label(text="No startup layout set", icon='INFO')
        elif _startup_state["done"]:
            layout.label(text="Startup layout ran this session", icon='CHECKMARK')
        elif _startup_state["scheduled"]:
            layout.label(text="Startup layout is waiting for the WM",
                         icon='TIME')

        layout.label(text=_store_path(), icon='FILE_FOLDER')
        layout.operator("wm.window_resizer_refresh", icon='FILE_REFRESH')
        if len(wm.window_resizer_monitors):
            box = layout.box()
            for m in wm.window_resizer_monitors:
                box.label(text="%s  %d\u00d7%d  at %d,%d"
                          % (m.name, m.w, m.h, m.x, m.y))
        else:
            layout.label(text="No monitors detected", icon='ERROR')


# ---------------------------------------------------------------- register
classes = (WRZ_Monitor, WRZ_Slot, WRZ_OT_refresh, WRZ_OT_grab, WRZ_OT_set,
           WRZ_OT_preset_save, WRZ_OT_preset_load, WRZ_OT_preset_delete,
           WRZ_OT_preset_startup, WRZ_OT_startup_now,
           WRZ_PT_panel, WRZ_PT_presets, WRZ_PT_options)


def _verbose_update(self, context):
    global VERBOSE
    VERBOSE = self.window_resizer_verbose


def register():
    for c in classes:
        bpy.utils.register_class(c)
    WM = bpy.types.WindowManager
    WM.window_resizer_slots = bpy.props.CollectionProperty(type=WRZ_Slot)
    WM.window_resizer_monitors = bpy.props.CollectionProperty(type=WRZ_Monitor)
    WM.window_resizer_preset = bpy.props.EnumProperty(
        name="Layout", items=_preset_items,
        description="Saved window layout")
    WM.window_resizer_verbose = bpy.props.BoolProperty(
        name="Log to Console", default=True, update=_verbose_update,
        description="Print each step and the geometry it produced")
    if not bpy.app.timers.is_registered(_sync):
        bpy.app.timers.register(_sync, first_interval=0.3, persistent=True)

    if not bpy.app.background:
        if _on_load_post not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_on_load_post)
        # If registration happens to run after load_post has already fired,
        # the handler will never see this launch, so arm it here as well.
        if _launching():
            _schedule_startup(STARTUP_DELAY)


def unregister():
    for fn in (_sync, _startup_apply):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    _startup_state["scheduled"] = False
    _enum_cache.clear()
    WM = bpy.types.WindowManager
    for attr in ("window_resizer_verbose", "window_resizer_preset",
                 "window_resizer_monitors", "window_resizer_slots"):
        if hasattr(WM, attr):
            delattr(WM, attr)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
