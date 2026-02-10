#!/usr/bin/env python3
import os
import sys
import ctypes

POSIX_FADV_DONTNEED = 4

def main():
    if len(sys.argv) != 2:
        print("usage: fadvise_drop.py /path/to/file", file=sys.stderr)
        return 2
    path = sys.argv[1]
    fd = os.open(path, os.O_RDONLY)
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        # int posix_fadvise(int fd, off_t offset, off_t len, int advice);
        rc = libc.posix_fadvise(fd, ctypes.c_longlong(0), ctypes.c_longlong(0), ctypes.c_int(POSIX_FADV_DONTNEED))
        if rc != 0:
            err = ctypes.get_errno()
            print(f"ERROR: posix_fadvise rc={rc} errno={err}", file=sys.stderr)
            return 1
        print("OK: posix_fadvise DONTNEED issued")
        return 0
    finally:
        os.close(fd)

if __name__ == "__main__":
    raise SystemExit(main())
