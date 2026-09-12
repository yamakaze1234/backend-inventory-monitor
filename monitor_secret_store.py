"""Windows user-bound protection; never exports or displays delivery credentials."""
import ctypes
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _convert(value, decrypt=False):
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming = Blob(len(value), buffer)
    outgoing = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(incoming), 'DingTalkSourceMonitor', None, None, None, 1,
                                    ctypes.byref(outgoing))
    if not ok:
        raise OSError('通知配置无法由当前 Windows 用户加密或解密。')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def protect(value):
    return _convert(value)


def unprotect(value):
    return _convert(value, decrypt=True)
