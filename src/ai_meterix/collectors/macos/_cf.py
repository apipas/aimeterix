"""Minimal ctypes bridge to CoreFoundation and IOKit.

Only what the readers need: create a CFString, convert CF values (strings,
numbers, booleans, data, arrays, dictionaries) to Python, release objects, and
read IOKit registry properties. Libraries are loaded on first use, so importing
this module on another OS is harmless.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from ctypes import (
    POINTER,
    byref,
    c_bool,
    c_char_p,
    c_double,
    c_int,
    c_int64,
    c_long,
    c_uint32,
    c_ulong,
    c_void_p,
)
from functools import cache
from typing import Any

_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
_UTF8 = 0x08000100
_SINT64 = 4
_FLOAT64 = 6
_MAX_DEPTH = 12


def _fn(lib: ctypes.CDLL, name: str, restype: Any, *argtypes: Any) -> None:
    func = getattr(lib, name)
    func.restype = restype
    func.argtypes = list(argtypes)


@cache
def cf() -> ctypes.CDLL:
    lib = ctypes.CDLL(_CF_PATH)
    _fn(lib, "CFRelease", None, c_void_p)
    _fn(lib, "CFGetTypeID", c_ulong, c_void_p)
    for name in ("String", "Number", "Boolean", "Data", "Array", "Dictionary"):
        _fn(lib, f"CF{name}GetTypeID", c_ulong)
    _fn(lib, "CFStringCreateWithCString", c_void_p, c_void_p, c_char_p, c_uint32)
    _fn(lib, "CFStringGetLength", c_long, c_void_p)
    _fn(lib, "CFStringGetMaximumSizeForEncoding", c_long, c_long, c_uint32)
    _fn(lib, "CFStringGetCString", c_bool, c_void_p, c_char_p, c_long, c_uint32)
    _fn(lib, "CFNumberIsFloatType", c_bool, c_void_p)
    _fn(lib, "CFNumberGetValue", c_bool, c_void_p, c_int, c_void_p)
    _fn(lib, "CFBooleanGetValue", c_bool, c_void_p)
    _fn(lib, "CFDataGetLength", c_long, c_void_p)
    _fn(lib, "CFDataGetBytePtr", c_void_p, c_void_p)
    _fn(lib, "CFArrayGetCount", c_long, c_void_p)
    _fn(lib, "CFArrayGetValueAtIndex", c_void_p, c_void_p, c_long)
    _fn(lib, "CFDictionaryGetCount", c_long, c_void_p)
    _fn(lib, "CFDictionaryGetKeysAndValues", None, c_void_p, POINTER(c_void_p), POINTER(c_void_p))
    _fn(lib, "CFDictionaryGetValue", c_void_p, c_void_p, c_void_p)
    _fn(lib, "CFDictionaryCreateMutableCopy", c_void_p, c_void_p, c_long, c_void_p)
    return lib


@cache
def _type_ids() -> dict[int, str]:
    lib = cf()
    return {
        lib.CFStringGetTypeID(): "string",
        lib.CFNumberGetTypeID(): "number",
        lib.CFBooleanGetTypeID(): "boolean",
        lib.CFDataGetTypeID(): "data",
        lib.CFArrayGetTypeID(): "array",
        lib.CFDictionaryGetTypeID(): "dict",
    }


def release(ref: int | None) -> None:
    if ref:
        cf().CFRelease(ref)


def cfstr(text: str) -> int:
    """A new CFString; the caller releases it."""
    ref = cf().CFStringCreateWithCString(None, text.encode(), _UTF8)
    if not ref:
        raise OSError(f"cannot create CFString for {text!r}")
    return int(ref)


def to_python(ref: int | None, depth: int = 0) -> Any:
    """Convert a CF value to Python. Unknown types become None. Does not release ``ref``."""
    if not ref or depth > _MAX_DEPTH:
        return None
    lib = cf()
    kind = _type_ids().get(lib.CFGetTypeID(ref))
    if kind == "string":
        size = lib.CFStringGetMaximumSizeForEncoding(lib.CFStringGetLength(ref), _UTF8) + 1
        buf = ctypes.create_string_buffer(size)
        if not lib.CFStringGetCString(ref, buf, size, _UTF8):
            return None
        return buf.value.decode("utf-8", "replace")
    if kind == "number":
        if lib.CFNumberIsFloatType(ref):
            fval = c_double()
            lib.CFNumberGetValue(ref, _FLOAT64, byref(fval))
            return fval.value
        ival = c_int64()
        lib.CFNumberGetValue(ref, _SINT64, byref(ival))
        return ival.value
    if kind == "boolean":
        return bool(lib.CFBooleanGetValue(ref))
    if kind == "data":
        length = lib.CFDataGetLength(ref)
        ptr = lib.CFDataGetBytePtr(ref)
        return ctypes.string_at(ptr, length) if ptr and length else b""
    if kind == "array":
        return [to_python(item, depth + 1) for item in iter_array(ref)]
    if kind == "dict":
        count = lib.CFDictionaryGetCount(ref)
        keys = (c_void_p * count)()
        values = (c_void_p * count)()
        lib.CFDictionaryGetKeysAndValues(ref, keys, values)
        out: dict[str, Any] = {}
        for key, value in zip(keys, values, strict=True):
            name = to_python(key, depth + 1)
            if isinstance(name, str):
                out[name] = to_python(value, depth + 1)
        return out
    return None


def iter_array(ref: int) -> Iterator[int]:
    lib = cf()
    for index in range(lib.CFArrayGetCount(ref)):
        yield int(lib.CFArrayGetValueAtIndex(ref, index) or 0)


def dict_get(ref: int, key: str) -> int | None:
    """Borrowed value for ``key`` in a CFDictionary (not to be released)."""
    name = cfstr(key)
    try:
        value = cf().CFDictionaryGetValue(ref, name)
    finally:
        release(name)
    return int(value) if value else None


@cache
def iokit() -> ctypes.CDLL:
    lib = ctypes.CDLL(_IOKIT_PATH)
    _fn(lib, "IOServiceMatching", c_void_p, c_char_p)
    _fn(lib, "IOServiceGetMatchingServices", c_int, c_uint32, c_void_p, POINTER(c_uint32))
    _fn(lib, "IOIteratorNext", c_uint32, c_uint32)
    _fn(lib, "IOObjectRelease", c_int, c_uint32)
    _fn(lib, "IORegistryEntryFromPath", c_uint32, c_uint32, c_char_p)
    _fn(lib, "IORegistryEntryGetChildIterator", c_int, c_uint32, c_char_p, POINTER(c_uint32))
    _fn(lib, "IORegistryEntryCreateCFProperty", c_void_p, c_uint32, c_void_p, c_void_p, c_uint32)
    _fn(
        lib,
        "IORegistryEntryCreateCFProperties",
        c_int,
        c_uint32,
        POINTER(c_void_p),
        c_void_p,
        c_uint32,
    )
    return lib


def _drain(iterator: int) -> Iterator[int]:
    lib = iokit()
    try:
        while entry := lib.IOIteratorNext(iterator):
            yield entry
    finally:
        lib.IOObjectRelease(iterator)


def properties(entry: int) -> dict[str, Any]:
    """All registry properties of an IOKit entry, as Python values."""
    props = c_void_p()
    if iokit().IORegistryEntryCreateCFProperties(entry, byref(props), None, 0) != 0:
        return {}
    try:
        value = to_python(props.value)
    finally:
        release(props.value)
    return value if isinstance(value, dict) else {}


def entry_property(entry: int, key: str) -> Any:
    """One registry property of an IOKit entry; cheaper than ``properties``."""
    name = cfstr(key)
    try:
        ref = iokit().IORegistryEntryCreateCFProperty(entry, name, None, 0)
    finally:
        release(name)
    try:
        return to_python(ref)
    finally:
        release(ref)


def matching(class_name: str) -> Iterator[int]:
    """IOKit services of a class. The caller releases each entry with ``free``."""
    lib = iokit()
    match = lib.IOServiceMatching(class_name.encode())
    if not match:
        return
    iterator = c_uint32()
    # IOServiceGetMatchingServices consumes the matching dictionary.
    if lib.IOServiceGetMatchingServices(0, match, byref(iterator)) != 0:
        return
    yield from _drain(iterator.value)


def children(entry: int, plane: str = "IOService") -> Iterator[int]:
    iterator = c_uint32()
    if iokit().IORegistryEntryGetChildIterator(entry, plane.encode(), byref(iterator)) != 0:
        return
    yield from _drain(iterator.value)


def from_path(path: str) -> int:
    """Registry entry at ``path`` (e.g. ``IODeviceTree:/cpus``), or 0."""
    return int(iokit().IORegistryEntryFromPath(0, path.encode()))


def free(entry: int) -> None:
    if entry:
        iokit().IOObjectRelease(entry)
