"""Windows 8+ recycle-only file operation; never fall back to permanent delete.

Uses IFileOperation FOFX_RECYCLEONDELETE and FOFX_EARLYFAILURE.
https://learn.microsoft.com/windows/win32/api/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags
"""
import ctypes
from pathlib import Path
import os
import uuid


def recycle_file(path):
    path=Path(path).resolve()
    if os.name!='nt' or not path.is_file():raise OSError('回收站仅支持Windows本地文件')
    HRESULT=ctypes.c_long
    pointer=ctypes.c_void_p
    class GUID(ctypes.Structure):
        _fields_=[('bytes',ctypes.c_ubyte*16)]
    def guid(value):return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)
    def check(value):
        if value<0:raise OSError('回收站操作失败，未执行永久删除')
    def method(obj,index,restype,*argtypes):
        table=ctypes.cast(obj,ctypes.POINTER(ctypes.POINTER(pointer))).contents
        return ctypes.WINFUNCTYPE(restype,pointer,*argtypes)(table[index])
    ole=ctypes.OleDLL('ole32')
    shell=ctypes.WinDLL('shell32')
    ole.CoInitializeEx.argtypes=[pointer,ctypes.c_uint];ole.CoInitializeEx.restype=HRESULT
    ole.CoCreateInstance.argtypes=[ctypes.POINTER(GUID),pointer,ctypes.c_uint,ctypes.POINTER(GUID),ctypes.POINTER(pointer)]
    ole.CoCreateInstance.restype=HRESULT
    shell.SHCreateItemFromParsingName.argtypes=[ctypes.c_wchar_p,pointer,ctypes.POINTER(GUID),ctypes.POINTER(pointer)]
    shell.SHCreateItemFromParsingName.restype=HRESULT
    operation=pointer();item=pointer()
    check(ole.CoInitializeEx(None,2))
    try:
        clsid=guid('3ad05575-8857-4850-9277-11b85bdb8e09')
        iid=guid('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')
        check(ole.CoCreateInstance(ctypes.byref(clsid),None,1,ctypes.byref(iid),ctypes.byref(operation)))
        shell_iid=guid('43826d1e-e718-42ee-bc55-a1e261c37bfe')
        check(shell.SHCreateItemFromParsingName(str(path),None,ctypes.byref(shell_iid),ctypes.byref(item)))
        # Silent/no confirmation/no error UI; stop on failure; recycle and record undo.
        check(method(operation,5,HRESULT,ctypes.c_uint)(operation,0x20180414))
        check(method(operation,18,HRESULT,pointer,pointer)(operation,item,None))
        check(method(operation,21,HRESULT)(operation))
        aborted=ctypes.c_int()
        check(method(operation,22,HRESULT,ctypes.POINTER(ctypes.c_int))(operation,ctypes.byref(aborted)))
        if aborted.value or path.exists():raise OSError('未能移入回收站，未强制删除')
    finally:
        if item:method(item,2,ctypes.c_ulong)(item)
        if operation:method(operation,2,ctypes.c_ulong)(operation)
        ole.CoUninitialize()
