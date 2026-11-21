"""Size and compression limits shared by archive writers."""

from io import BufferedReader
import zipfile

MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ZIP_DIRECTORY_BYTES = 4 * 1024 * 1024
MAX_ZIP_ENTRIES = 12_000
COPY_CHUNK_BYTES = 64 * 1024
SAFE_ZIP_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class BoundedZipReader(BufferedReader):
    def __init__(self, raw, max_read: int = MAX_ZIP_DIRECTORY_BYTES):
        super().__init__(raw)
        self.max_read = max_read

    def read(self, size: int = -1) -> bytes:
        # ZipFile loads the central directory in one read before exposing entries.
        if size > self.max_read:
            raise ValueError("ZIP directory exceeds the size limit")
        data = super().read(self.max_read + 1 if size < 0 else size)
        if len(data) > self.max_read:
            raise ValueError("ZIP directory exceeds the size limit")
        return data
