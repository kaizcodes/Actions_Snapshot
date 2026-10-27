bl_info = {
    "name": "Action Snapshots",
    "author": "",
    "version": (2, 0, 0),
    "blender": (4, 0, 0),
    "location": "Edit > Preferences > Add-ons",
    "description": "Saves a .blend copy for every new entry in the Undo History, named '<session> + <action>'",
    "category": "System",
}

# Blender's Python API cannot list Undo History entries (ed.undo_history only takes an index and
# there is no undo-push handler). The history lives in the C-side UndoStack that hangs off the
# window manager, so this add-on reads it directly from the process's own memory:
#
#   UndoStack { ListBase steps; UndoStep *step_active; UndoStep *step_init; }
#   UndoStep  { UndoStep *next, *prev; char name[64]; ... }
#
# The pointer path wm -> UndoStack is found by scanning and validating the structure, so no
# Blender-version-specific offsets are hard-coded. All reads go through an OS call that fails
# cleanly on a bad address (it can never crash Blender). If the structure cannot be found the
# add-on does nothing and says so in the system console.
#
# Tested on Windows with Blender 5.2. The Linux and macOS readers follow the documented
# process_vm_readv / mach_vm_read_overwrite signatures but have not been run.

import ctypes
import os
import re
import struct
import sys
import time
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

POLL_INTERVAL = 0.1  # seconds between undo stack checks
RESCAN_INTERVAL = 1.0  # minimum seconds between searches for the undo stack
MAX_SAVE_ATTEMPTS = 20
_NAME_OFFSET = 16  # UndoStep.name, after next/prev
_NAME_SIZE = 64
_SKIP_OFFSET = 96  # UndoStep.skip: "users should never see this step"
_MAX_STEPS = 10000
_PREFIX = "[Action Snapshots]"


# --------------------------------------------------------------------------- memory reading


def _build_reader():
    if sys.maxsize <= 2**32:
        raise RuntimeError("32-bit Blender is not supported")

    if sys.platform == "win32":
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k32.ReadProcessMemory.restype = wintypes.BOOL
        process = k32.GetCurrentProcess()

        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_size_t(0)
            ok = k32.ReadProcessMemory(process, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
            return buf.raw if ok and got.value == size else None

        return read

    if sys.platform.startswith("linux"):

        class IOVec(ctypes.Structure):
            _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]

        libc = ctypes.CDLL(None, use_errno=True)
        libc.process_vm_readv.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(IOVec),
            ctypes.c_ulong,
            ctypes.POINTER(IOVec),
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.process_vm_readv.restype = ctypes.c_ssize_t
        pid = os.getpid()

        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            local = IOVec(ctypes.cast(buf, ctypes.c_void_p), size)
            remote = IOVec(ctypes.c_void_p(addr), size)
            got = libc.process_vm_readv(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
            return buf.raw if got == size else None

        return read

    if sys.platform == "darwin":
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        task = ctypes.c_uint.in_dll(lib, "mach_task_self_").value
        lib.mach_vm_read_overwrite.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        lib.mach_vm_read_overwrite.restype = ctypes.c_int

        def read(addr, size):
            buf = ctypes.create_string_buffer(size)
            got = ctypes.c_uint64(0)
            status = lib.mach_vm_read_overwrite(task, addr, size, ctypes.addressof(buf), ctypes.byref(got))
            return buf.raw if status == 0 and got.value == size else None

        return read

    raise RuntimeError(f"unsupported platform {sys.platform}")


_read = None


def _u64(addr, offset=0):
    addr += offset
    if addr < 0x10000 or addr > 0x7FFFFFFFFFFF:
        return None
    raw = _read(addr, 8)
    return struct.unpack("<Q", raw)[0] if raw else None


# --------------------------------------------------------------------------- undo stack


def _step_name(step):
    raw = _read(step + _NAME_OFFSET, _NAME_SIZE)
    if not raw:
        return None
    try:
        text = raw.split(b"\0", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text and text.isprintable() else None


def _step_skipped(step):
    raw = _read(step + _SKIP_OFFSET, 1)
    return bool(raw and raw[0] == 1)


def _read_chain(stack):
    """(step pointers oldest->newest, active step) if `stack` is a valid UndoStack, else None."""
    first = _u64(stack)
    last = _u64(stack, 8)
    active = _u64(stack, 16)
    if not first or not last or active is None or _u64(first, 8) != 0:
        return None
    pointers = []
    seen = set()
    step = first
    while step:
        if step in seen or len(pointers) >= _MAX_STEPS:
            return None
        seen.add(step)
        pointers.append(step)
        step = _u64(step)
        if step is None:
            return None
    if pointers[-1] != last or (active and active not in seen):
        return None
    return pointers, active


def _is_undo_stack(addr):
    chain = _read_chain(addr)
    if not chain or not chain[1]:
        return None
    if any(_step_name(step) is None for step in chain[0]):
        return None
    return chain


def _wm_address():
    return bpy.data.window_managers[0].as_pointer()


def _resolve(path):
    addr = _wm_address()
    for offset in path:
        addr = _u64(addr, offset)
        if not addr:
            return None
    return addr


def _locate():
    """Search wm (and one level of pointers below it) for a structure shaped like an UndoStack."""
    wm = _wm_address()
    best_path, best_len = None, 0
    for off1 in range(0, 4096, 8):
        level1 = _u64(wm, off1)
        if not level1:
            continue
        chain = _is_undo_stack(level1)
        if chain and len(chain[0]) > best_len:
            best_path, best_len = (off1,), len(chain[0])
        block = _read(level1, 2048) if 0x10000 <= level1 <= 0x7FFFFFFFFFFF else None
        if not block:
            continue
        for off2 in range(0, 2048, 8):
            level2 = struct.unpack_from("<Q", block, off2)[0]
            if level2 < 0x10000 or level2 > 0x7FFFFFFFFFFF:
                continue
            chain = _is_undo_stack(level2)
            if chain and len(chain[0]) > best_len:
                best_path, best_len = (off1, off2), len(chain[0])
    return best_path


# --------------------------------------------------------------------------- state

_path = None  # offsets from wm to the UndoStack pointer
_pointers = None  # step pointers at the last sync; None = resync silently on next poll
_active = None
_last_scan = 0.0
_warned = False
_pending = []  # [(name, attempts)] snapshots still to save


def _stack_address(now):
    global _path, _last_scan, _pointers, _warned
    if _path:
        addr = _resolve(_path)
        if addr and _read_chain(addr):
            return addr
    if now - _last_scan < RESCAN_INTERVAL:
        return None
    _last_scan = now
    path = _locate()
    if path is None:
        if not _warned:
            print(f"{_PREFIX} could not find Blender's undo stack in this build; snapshots disabled.")
            _warned = True
        return None
    if path != _path:
        _path, _pointers = path, None
    _warned = False
    return _resolve(_path)


def _sync():
    """Adopt the current undo stack as the baseline without saving anything."""
    global _pointers, _active
    stack = _stack_address(time.monotonic())
    chain = _read_chain(stack) if stack else None
    if chain:
        _pointers, _active = chain
    else:
        _pointers = None


# --------------------------------------------------------------------------- saving

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean(text):
    text = _INVALID.sub("_", text).strip(" .")
    return text[:100] or "Action"


def _session_name():
    return Path(bpy.data.filepath).stem if bpy.data.filepath else "Untitled"


def _snapshot_dir():
    if bpy.data.filepath:
        root = Path(bpy.data.filepath).parent / "action_snapshots"
    else:
        root = Path(bpy.app.tempdir or os.path.expanduser("~")) / "action_snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save(action):
    directory = _snapshot_dir()
    stem = f"{_clean(_session_name())} + {_clean(action)}"
    path = directory / f"{stem}.blend"
    number = 2
    while path.exists():
        path = directory / f"{stem} ({number}).blend"
        number += 1
    bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False, copy=True)
    print(f"{_PREFIX} saved {path}")


def _flush_pending():
    global _pending
    remaining = []
    for name, attempts in _pending:
        try:
            _save(name)
        except Exception as exc:
            if attempts + 1 < MAX_SAVE_ATTEMPTS:
                remaining.append((name, attempts + 1))
            else:
                print(f"{_PREFIX} giving up on '{name}': {exc!r}")
    _pending = remaining


# --------------------------------------------------------------------------- polling


def _poll():
    global _pointers, _active
    try:
        stack = _stack_address(time.monotonic())
        chain = _read_chain(stack) if stack else None
        if not chain:
            _pointers = None
            return POLL_INTERVAL
        pointers, active = chain
        if _pointers is None:
            _pointers, _active = pointers, active
        elif pointers != _pointers or active != _active:
            # Undo / redo / history jumps resync in their handlers, so anything that changed
            # since the last sync is a push. A push that reused a freed step's address leaves
            # the chain unchanged; it is recognisable because the active step is the newest.
            known = set(_pointers)
            new = [step for step in pointers if step not in known]
            if not new and active == pointers[-1]:
                new = [active]
            _pointers, _active = pointers, active
            for step in new:
                name = _step_name(step)
                if name is not None and not _step_skipped(step):
                    _pending.append((name, 0))
        if _pending:
            _flush_pending()
    except Exception as exc:
        print(f"{_PREFIX} {exc!r}")
    return POLL_INTERVAL


@persistent
def _on_undo_redo(*_args):
    try:
        _sync()
    except Exception as exc:
        print(f"{_PREFIX} {exc!r}")


@persistent
def _on_load(*_args):
    global _pointers
    _pointers = None
    _pending.clear()


# --------------------------------------------------------------------------- registration


def register():
    global _read, _path, _pointers, _warned, _last_scan
    _read = _build_reader()
    _path, _pointers, _warned, _last_scan = None, None, False, 0.0
    _pending.clear()
    for handlers, func in (
        (bpy.app.handlers.undo_post, _on_undo_redo),
        (bpy.app.handlers.redo_post, _on_undo_redo),
        (bpy.app.handlers.load_post, _on_load),
    ):
        if func not in handlers:
            handlers.append(func)
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=POLL_INTERVAL, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
    for handlers, func in (
        (bpy.app.handlers.undo_post, _on_undo_redo),
        (bpy.app.handlers.redo_post, _on_undo_redo),
        (bpy.app.handlers.load_post, _on_load),
    ):
        if func in handlers:
            handlers.remove(func)
