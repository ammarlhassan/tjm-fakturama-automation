"""Read install4j's embedded EXE from an MSI without executing Windows Installer."""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

msi = ctypes.WinDLL("msi")
handle = wintypes.UINT
msi.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(handle)]
msi.MsiDatabaseOpenViewW.argtypes = [handle, wintypes.LPCWSTR, ctypes.POINTER(handle)]
msi.MsiViewExecute.argtypes = [handle, handle]
msi.MsiViewFetch.argtypes = [handle, ctypes.POINTER(handle)]
msi.MsiRecordReadStream.argtypes = [
    handle,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
]


def check(code):
    if code:
        raise OSError(f"MSI read failed: {code}")


if __name__ == "__main__":
    db, view, record = handle(), handle(), handle()
    check(msi.MsiOpenDatabaseW(str(Path(sys.argv[1]).resolve()), None, ctypes.byref(db)))
    try:
        check(
            msi.MsiDatabaseOpenViewW(
                db, "SELECT `Data` FROM `Binary` WHERE `Name`='Executable'", ctypes.byref(view)
            )
        )
        check(msi.MsiViewExecute(view, 0))
        check(msi.MsiViewFetch(view, ctypes.byref(record)))
        target = Path(sys.argv[2])
        with target.open("wb") as output:
            while True:
                size = wintypes.DWORD(1024 * 1024)
                buffer = ctypes.create_string_buffer(size.value)
                check(msi.MsiRecordReadStream(record, 1, buffer, ctypes.byref(size)))
                if not size.value:
                    break
                output.write(buffer.raw[: size.value])
        print(f"Extracted {target.name}: {target.stat().st_size:,} bytes")
    finally:
        for resource in (record, view, db):
            if resource.value:
                msi.MsiCloseHandle(resource)
