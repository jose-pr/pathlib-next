from __future__ import annotations

import io as _io
import shutil as _shutil
import typing as _ty

from .. import utils as _utils


class BinaryOpen(_ty.Protocol):
    """Protocol for objects that support open->io.IoBase"""

    __slots__ = ()

    @_utils.notimplemented
    def _open(
        self,
        mode="r",
        buffering=-1,
    ) -> _io.IOBase:
        """
        All operations should be binary
        To be used only by open() to obtain binary stream to provide implementations for all methods
        """
        ...

    def open(
        self,
        mode="r",
        buffering=-1,
        encoding: str = None,
        errors: str = None,
        newline: str = None,
    ) -> _io.IOBase:
        """
        Open the a handle to an object that implement io.IOBase
        """
        fh = self._open(mode.replace("b", ""), buffering)
        if "b" not in mode:
            # io.text_encoding is 3.10+; on 3.9 pass encoding through as-is
            # (None means locale default, same effective behavior).
            encoding = getattr(_io, "text_encoding", lambda e, stacklevel=1: e)(
                encoding
            )
            fh = _io.TextIOWrapper(fh, encoding, errors, newline)
        return fh

    def read_bytes(self) -> bytes:
        """
        Open in bytes mode, read it, and close the file.
        """
        with self.open(mode="rb") as f:
            return f.read()

    def read_text(
        self, encoding: str = None, errors: str = None, newline: str = None
    ) -> str:
        """
        Open in text mode, read it, and close the file. (newline= is 3.13
        parity.)
        """
        with self.open(
            mode="r", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.read()

    def write_bytes(self, data: bytes):
        """
        Open in bytes mode, write to it, and close the file.
        """
        # type-check for the buffer interface before truncating the file
        view = memoryview(data)
        with self.open(mode="wb") as f:
            return f.write(view)

    def write_text(
        self, data: str, encoding: str = None, errors: str = None, newline: str = None
    ):
        """
        Open in text mode, write to it, and close the file.
        """
        if not isinstance(data, str):
            raise TypeError("data must be str, not %s" % data.__class__.__name__)
        with self.open(
            mode="w", encoding=encoding, errors=errors, newline=newline
        ) as f:
            return f.write(data)

    def copy(
        self,
        target: "BinaryOpen",
        *,
        progress: "_ty.Callable[[int, _ty.Optional[int]], None]" = None,
        chunk_size: int = _shutil.COPY_BUFSIZE,
    ):
        """Copy the binary content from this object to a target object.

        `progress`, when given, is called after each chunk is written as
        `progress(bytes_copied, total_size)`: `bytes_copied` increases
        monotonically and equals `total_size` (if known) after the final
        call. `total_size` is this object's `stat().st_size` when `self`
        also implements the `Stat` protocol and `stat()` succeeds,
        otherwise `None` -- `BinaryOpen` alone has no size concept.
        `chunk_size` controls how many bytes are read per iteration
        (default: `shutil.COPY_BUFSIZE`). With `progress=None` (the
        default), behavior and bytes-on-wire are identical to before this
        was added (a plain `shutil.copyfileobj`).
        """
        if progress is None:
            with target.open("wb") as output, self.open("rb") as input:
                _shutil.copyfileobj(input, output, chunk_size)
            return

        total_size = None
        try:
            total_size = self.stat().st_size
        except (AttributeError, NotImplementedError, OSError):
            total_size = None

        copied = 0
        with target.open("wb") as output, self.open("rb") as input:
            while chunk := input.read(chunk_size):
                output.write(chunk)
                copied += len(chunk)
                progress(copied, total_size)
