"""Incremental parser for a `multipart/x-mixed-replace` MJPEG stream.

Kept separate from the upstream connection so it can be tested on bytes
rather than on a socket, and so the awkward parts of the format are in one
place:

* The boundary is only in the `Content-Type` response header, never assumed.
  Home Assistant's camera_proxy_stream has historically advertised a boundary
  that already starts with `--` (aiohttp's MultipartWriter is handed the
  literal string `--frameboundary`), which makes the on-the-wire delimiter
  `----frameboundary`. Hard-coding either spelling breaks the moment the
  other one is served, so the delimiter is always derived as `--` + whatever
  the header said.
* Parts may or may not carry `Content-Length`. With it, the body is read by
  length; without it, the body runs to the next delimiter.
* Chunks arrive at arbitrary boundaries, so every state has to survive being
  fed one byte at a time.

Memory is bounded in both directions: a stream that never produces a
delimiter is given up on, and so is a single part that never ends.
"""

# Generous enough for a full-resolution JPEG, small enough that a stream of
# junk can't exhaust the container's memory before it is given up on.
MAX_BUFFER_BYTES = 8 * 1024 * 1024

_HEADER_END = b"\r\n\r\n"
_JPEG_SOI = b"\xff\xd8"


class MjpegError(Exception):
    """The stream isn't parseable - reconnect rather than keep reading."""


def boundary_from_content_type(content_type: str | None) -> str | None:
    """The boundary token out of a Content-Type header, or None.

    Returns it exactly as sent, quotes stripped and nothing else normalised;
    the caller prefixes `--` to build the delimiter.
    """
    if not content_type:
        return None
    parts = content_type.split(";")
    if "multipart/" not in parts[0].lower():
        return None
    for param in parts[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "boundary":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


class MjpegParser:
    """Feed it response bytes, get whole JPEG frames back."""

    _SEEK = "seek"
    _HEADERS = "headers"
    _BODY = "body"

    def __init__(self, boundary: str, max_buffer_bytes: int = MAX_BUFFER_BYTES):
        if not boundary:
            raise ValueError("boundary is required")
        self._delimiter = b"--" + boundary.encode("latin-1")
        self._buf = bytearray()
        self._state = self._SEEK
        self._expected: int | None = None
        self._max_buffer = max_buffer_bytes
        # Bytes thrown away looking for a delimiter that never came. The
        # buffer itself stays small while seeking (only a partial delimiter
        # is kept), so this is what notices a stream of junk.
        self._skipped = 0
        self.closed = False

    def feed(self, chunk: bytes) -> list[bytes]:
        """Append `chunk` and return every frame it completed.

        Raises MjpegError when the stream stops looking like the multipart
        body that was asked for, which the caller treats as a reconnect.
        """
        if self.closed:
            return []
        if chunk:
            self._buf.extend(chunk)

        frames: list[bytes] = []
        while not self.closed:
            progressed, frame = self._step()
            if frame is not None and frame.startswith(_JPEG_SOI):
                # A part that isn't a JPEG is dropped rather than forwarded: a
                # viewer's <img> would show a broken frame, and an error body
                # served mid-stream is exactly the case this guards.
                frames.append(bytes(frame))
            if not progressed:
                break
        return frames

    def _step(self) -> tuple[bool, bytearray | None]:
        """Advance one state transition.

        Returns (progressed, frame): `progressed` is False when the buffer
        doesn't hold enough to go any further, and `frame` is set only on the
        transition that completes a part.
        """
        if self._state == self._SEEK:
            index = self._buf.find(self._delimiter)
            if index < 0:
                # Keep only what could still be the head of a delimiter split
                # across two chunks; everything before it can go.
                keep = len(self._delimiter) - 1
                if len(self._buf) > keep:
                    dropped = len(self._buf) - keep
                    del self._buf[:dropped]
                    self._skipped += dropped
                    if self._skipped > self._max_buffer:
                        raise MjpegError(
                            f"no boundary in the first {self._skipped} bytes - "
                            "this doesn't look like an MJPEG stream"
                        )
                return False, None
            del self._buf[: index + len(self._delimiter)]
            self._skipped = 0
            self._state = self._HEADERS
            return True, None

        if self._state == self._HEADERS:
            # The closing delimiter is `--boundary--`; anything after it is
            # epilogue, not frames.
            if self._buf[:2] == b"--":
                self.closed = True
                return False, None
            if len(self._buf) < 2:
                return False, None
            index = self._buf.find(_HEADER_END)
            if index < 0:
                if len(self._buf) > self._max_buffer:
                    raise MjpegError("part headers never ended")
                return False, None
            headers = bytes(self._buf[:index])
            del self._buf[: index + len(_HEADER_END)]
            self._expected = _content_length(headers)
            self._state = self._BODY
            return True, None

        # self._BODY
        if self._expected is not None:
            if len(self._buf) < self._expected:
                return False, None
            frame = self._buf[: self._expected]
            del self._buf[: self._expected]
            self._state = self._SEEK
            self._expected = None
            return True, frame

        # No Content-Length: the body runs up to the next delimiter.
        index = self._buf.find(self._delimiter)
        if index < 0:
            if len(self._buf) > self._max_buffer:
                raise MjpegError("part exceeded the buffer cap with no delimiter in sight")
            return False, None
        frame = self._buf[:index]
        del self._buf[: index + len(self._delimiter)]
        self._state = self._HEADERS
        # Strip the CRLF that separates the body from the delimiter.
        while frame[-1:] in (b"\n", b"\r"):
            del frame[-1:]
        return True, frame


def _content_length(headers: bytes) -> int | None:
    for line in headers.split(b"\r\n"):
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None
            return length if length >= 0 else None
    return None
