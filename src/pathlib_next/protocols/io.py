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
            encoding = _io.text_encoding(encoding)
            fh = _io.TextIOWrapper(fh, encoding, errors, newline)
        return fh

    def read_bytes(self) -> bytes:
        """
        Open in bytes mode, read it, and close the file.
        """
        with self.open(mode="rb") as f:
            return f.read()

    def read_text(self, encoding: str = None, errors: str = None) -> str:
        """
        Open in text mode, read it, and close the file.
        """
        with self.open(mode="r", encoding=encoding, errors=errors) as f:
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

    def copy(self, target: "BinaryOpen"):
        with target.open("wb") as output, self.open("rb") as input:
            _shutil.copyfileobj(input, output)
