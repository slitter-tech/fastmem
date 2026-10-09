class FastMemError(Exception):
    """Base class for all fastmem errors.

    The message is built lazily in ``__str__`` rather than in the
    constructor: while nobody prints the exception, there is no reason to
    pay for string formatting. This matters in hot loops, where creating
    the error object costs ~0.4 us instead of the ~3 us a formatted
    message would take.
    """


class ProcessOpenError(FastMemError):
    """Failed to open a process.

    :ivar pid: PID of the process that could not be opened.
    :ivar code: Windows error code (``GetLastError``).
    """

    __slots__ = ("pid", "code")

    def __init__(self, pid: int, code: int = 0) -> None:
        self.pid = pid
        self.code = code
        self.args = (pid, code)

    def __str__(self) -> str:
        from ._winapi import OPEN_ERROR_HINTS

        hint = OPEN_ERROR_HINTS.get(self.code, "неизвестная ошибка")
        return "Не удалось открыть процесс {}: {} (код Windows {})".format(
            self.pid, hint, self.code
        )


class ReadMemoryError(FastMemError):
    """Failed to read process memory.

    :ivar address: address that could not be read.
    :ivar size: requested read size in bytes.
    :ivar code: Windows error code (``GetLastError``).
    """

    __slots__ = ("address", "size", "code")

    def __init__(self, address: int = 0, size: int = 0, code: int = 0) -> None:
        self.address = address
        self.size = size
        self.code = code
        self.args = (address, size, code)

    def __str__(self) -> str:
        from ._winapi import ERROR_HINTS

        hint = ERROR_HINTS.get(self.code, "неизвестная ошибка")
        if not self.address:
            return "Чтение памяти не удалось: {} (код Windows {})".format(
                hint, self.code
            )
        return (
            "Чтение памяти не удалось: 0x{:X} ({} байт): {} "
            "(код Windows {})".format(self.address, self.size, hint, self.code)
        )


class ProcessTerminatedError(ReadMemoryError):
    """The target process has exited, or its memory is no longer mapped.

    Subclasses :class:`ReadMemoryError`, so code that catches
    ``ReadMemoryError`` keeps working unchanged.
    """


class ProcessClosedError(ReadMemoryError):
    """Memory access attempted after :meth:`Process.close`.

    Subclasses :class:`ReadMemoryError` for backward compatibility.
    """

    __slots__ = ()

    def __str__(self) -> str:
        return "Процесс уже закрыт (handle освобождён)"
