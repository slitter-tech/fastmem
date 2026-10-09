"""Low-level ctypes wrappers around kernel32.

Every prototype is declared explicitly (argtypes/restype). That is needed
both for correct 64-bit pointer passing and for speed: without argtypes,
ctypes truncates Python ints to C int.

Anything introduced after Vista/7 is loaded optionally through getattr and
checked against None, so the library keeps working on Win7, Server 2008
and ARM64 without raising.
"""

import ctypes
from ctypes import CFUNCTYPE, wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# --------------------------------------------------------------------------
# Process access rights
# --------------------------------------------------------------------------

PROCESS_TERMINATE = 0x0001
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

# Minimum right set actually required for reading memory.
# PROCESS_QUERY_LIMITED_INFORMATION is needed only to detect the target
# bitness and, unlike PROCESS_QUERY_INFORMATION, is granted to every
# process owned by the same user.
ACCESS_READ_ONLY = PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION


# --------------------------------------------------------------------------
# Error codes (only the ones our calls actually produce)
# --------------------------------------------------------------------------

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_INVALID_PARAMETER = 87
ERROR_PARTIAL_COPY = 299

ERROR_HINTS = {
    ERROR_ACCESS_DENIED: (
        "access denied. Administrator rights are required, or the process "
        "is protected (PPL / Protected Process Light / anti-cheat)"
    ),
    ERROR_INVALID_HANDLE: (
        "invalid handle - the process has exited or was killed"
    ),
    ERROR_INVALID_PARAMETER: (
        "invalid parameter - the address is outside the process address "
        "space, or the size is malformed"
    ),
    ERROR_PARTIAL_COPY: (
        "partial copy - the range crosses into unmapped memory"
    ),
}

# OpenProcess reports the same numeric codes with different meanings, so
# it gets its own table: code 5 here is "no rights", and code 87 almost
# always means "no such PID".
OPEN_ERROR_HINTS = {
    ERROR_ACCESS_DENIED: (
        "access denied. Administrator rights are required, or the process "
        "is protected (PPL / Protected Process Light / anti-cheat)"
    ),
    ERROR_INVALID_PARAMETER: "invalid PID - no such process",
    ERROR_INVALID_HANDLE: "invalid PID or insufficient rights",
}


# --------------------------------------------------------------------------
# IMAGE_FILE_MACHINE_* - machine types, used for target bitness detection
# --------------------------------------------------------------------------

IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
IMAGE_FILE_MACHINE_I386 = 0x014C
IMAGE_FILE_MACHINE_ARM = 0x01C0
IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_FILE_MACHINE_ARM64 = 0xAA64
IMAGE_FILE_MACHINE_ARM64EC = 0xA641

MACHINE_NAMES = {
    IMAGE_FILE_MACHINE_UNKNOWN: "unknown",
    IMAGE_FILE_MACHINE_I386: "x86",
    IMAGE_FILE_MACHINE_ARM: "ARM",
    IMAGE_FILE_MACHINE_AMD64: "x64",
    IMAGE_FILE_MACHINE_ARM64: "ARM64",
    IMAGE_FILE_MACHINE_ARM64EC: "ARM64EC",
}

# Processor architectures from SYSTEM_INFO (not IMAGE_FILE_MACHINE_*)
_PROCESSOR_ARCHITECTURE_INTEL = 0
_PROCESSOR_ARCHITECTURE_ARM = 5
_PROCESSOR_ARCHITECTURE_AMD64 = 9
_PROCESSOR_ARCHITECTURE_ARM64 = 12

_ARCH_TO_MACHINE = {
    _PROCESSOR_ARCHITECTURE_INTEL: IMAGE_FILE_MACHINE_I386,
    _PROCESSOR_ARCHITECTURE_ARM: IMAGE_FILE_MACHINE_ARM,
    _PROCESSOR_ARCHITECTURE_AMD64: IMAGE_FILE_MACHINE_AMD64,
    _PROCESSOR_ARCHITECTURE_ARM64: IMAGE_FILE_MACHINE_ARM64,
}

# Architectures with 64-bit pointers
MACHINES_64BIT = frozenset(
    (
        IMAGE_FILE_MACHINE_AMD64,
        IMAGE_FILE_MACHINE_ARM64,
        IMAGE_FILE_MACHINE_ARM64EC,
    )
)


# --------------------------------------------------------------------------
# Core prototypes
# --------------------------------------------------------------------------

OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

# c_void_p rather than LPCVOID/LPVOID: it accepts both ints and ready-made
# objects, never truncates to 32 bits, and skips a type check.
ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [
    ctypes.c_void_p,   # hProcess
    ctypes.c_void_p,   # lpBaseAddress
    ctypes.c_void_p,   # lpBuffer
    ctypes.c_size_t,   # nSize
    ctypes.c_void_p,   # lpNumberOfBytesRead (may be NULL)
]
ReadProcessMemory.restype = wintypes.BOOL

GetExitCodeProcess = kernel32.GetExitCodeProcess
GetExitCodeProcess.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
]
GetExitCodeProcess.restype = wintypes.BOOL

STILL_ACTIVE = 259


def _optional(name, argtypes, restype):
    """Return a kernel32 function, or None when absent on this system."""
    fn = getattr(kernel32, name, None)
    if fn is None:
        return None
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


# IsWow64Process2 (Windows 10 1709 / Server 2019) is the best source: it
# reports both the process and the system machine, so x64, ARM64 and
# ARM64EC are told apart. None on older systems.
IsWow64Process2 = _optional(
    "IsWow64Process2",
    [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.WORD),
    ],
    wintypes.BOOL,
)

# IsWow64Process (Vista+) is the fallback: only a WOW64 flag.
IsWow64Process = _optional(
    "IsWow64Process",
    [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)],
    wintypes.BOOL,
)


# --------------------------------------------------------------------------
# Virtual memory: MEMORY_BASIC_INFORMATION and VirtualQueryEx
# --------------------------------------------------------------------------

MEM_COMMIT = 0x1000
MEM_FREE = 0x10000
MEM_RESERVE = 0x2000

MEM_PRIVATE = 0x20000
MEM_MAPPED = 0x40000
MEM_IMAGE = 0x1000000

PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

# Protection values that grant at least PROTECT_READ
PROT_READABLE = frozenset(
    (
        PAGE_READONLY,
        PAGE_READWRITE,
        PAGE_WRITECOPY,
        PAGE_EXECUTE_READ,
        PAGE_EXECUTE_READWRITE,
        PAGE_EXECUTE_WRITECOPY,
    )
)


def _define_mbi():
    """Describe MEMORY_BASIC_INFORMATION for the current Python bitness.

    RegionSize is SIZE_T, so it is 4 bytes wide under 32-bit Python and 8
    under 64-bit. No offsets are hardcoded: ctypes applies the platform
    alignment rules itself.
    """
    fields = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]
    return type("MEMORY_BASIC_INFORMATION", (ctypes.Structure,),
                {"_fields_": fields})


MEMORY_BASIC_INFORMATION = _define_mbi()
mbi_size = ctypes.sizeof(MEMORY_BASIC_INFORMATION)

# Shared structure instance: region enumeration reuses it instead of
# allocating one per VirtualQueryEx call.
shared_mbi = MEMORY_BASIC_INFORMATION()

VirtualQueryEx = _optional(
    "VirtualQueryEx",
    [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t],
    ctypes.c_size_t,
)


def _query_system_info(native: bool = False):
    """Fill and return the shared SYSTEM_INFO (native or current)."""
    fn = GetNativeSystemInfo if (native and GetNativeSystemInfo) else GetSystemInfo
    if fn is not None:
        fn(ctypes.byref(_system_info))
    return _system_info


class _SYSTEM_INFO(ctypes.Structure):
    """SYSTEM_INFO without hardcoded offsets - field order is stable across
    architectures and ctypes fills in the correct size."""

    _fields_ = [
        ("wProcessorArchitecture", wintypes.WORD),
        ("wReserved", wintypes.WORD),
        ("dwPageSize", wintypes.DWORD),
        ("lpMinimumApplicationAddress", ctypes.c_void_p),
        ("lpMaximumApplicationAddress", ctypes.c_void_p),
        ("dwActiveProcessorMask", ctypes.c_size_t),
        ("dwNumberOfProcessors", wintypes.DWORD),
        ("dwProcessorType", wintypes.DWORD),
        ("dwAllocationGranularity", wintypes.DWORD),
        ("wProcessorLevel", wintypes.WORD),
        ("wProcessorRevision", wintypes.WORD),
    ]


_system_info = _SYSTEM_INFO()

GetSystemInfo = _optional(
    "GetSystemInfo", [ctypes.POINTER(_SYSTEM_INFO)], None
)
# GetNativeSystemInfo (Vista+) reports the system architecture rather than
# the current process one, which matters when fastmem runs under 32-bit
# Python on x64.
GetNativeSystemInfo = _optional(
    "GetNativeSystemInfo", [ctypes.POINTER(_SYSTEM_INFO)], None
)


def native_machine() -> int:
    """IMAGE_FILE_MACHINE_* of the machine we are running on."""
    info = _query_system_info(native=True)
    return _ARCH_TO_MACHINE.get(
        info.wProcessorArchitecture, IMAGE_FILE_MACHINE_UNKNOWN
    )


def page_size() -> int:
    """System page size (4/16/64 KiB - read from Windows, not assumed)."""
    return int(_query_system_info().dwPageSize) or 4096
