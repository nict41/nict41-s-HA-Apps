"""The multipart parser, fed the awkward cases on purpose."""

import pytest

from conftest import JPEG, mjpeg_body
from mjpeg import MjpegError, MjpegParser, boundary_from_content_type


@pytest.mark.parametrize("header,expected", [
    ("multipart/x-mixed-replace; boundary=--frameboundary", "--frameboundary"),
    ("multipart/x-mixed-replace;boundary=frameboundary", "frameboundary"),
    ('multipart/x-mixed-replace; boundary="quoted-one"', "quoted-one"),
    ("multipart/x-mixed-replace; BOUNDARY=UpperCase", "UpperCase"),
    ("multipart/x-mixed-replace", None),
    ("image/jpeg", None),
    ("text/html; boundary=nope", None),
    (None, None),
])
def test_boundary_comes_from_the_header(header, expected):
    """Never hard-coded: HA's own boundary already starts with `--`, and a
    parser that assumed either spelling would break on the other."""
    assert boundary_from_content_type(header) == expected


def test_parses_frames_with_content_length():
    parser = MjpegParser("--frameboundary")
    frames = parser.feed(mjpeg_body([JPEG, JPEG]))
    assert frames == [JPEG, JPEG]


def test_parses_frames_without_content_length():
    parser = MjpegParser("x")
    frames = parser.feed(mjpeg_body([JPEG, JPEG, JPEG], boundary="x", with_length=False))
    # The last part has no following delimiter yet, so only the first two are
    # complete. That is the point of an incremental parser.
    assert frames == [JPEG, JPEG]


def test_survives_being_fed_one_byte_at_a_time():
    body = mjpeg_body([JPEG, JPEG], boundary="bnd")
    parser = MjpegParser("bnd")
    frames = []
    for index in range(len(body)):
        frames.extend(parser.feed(body[index:index + 1]))
    assert frames == [JPEG, JPEG]


def test_delimiter_split_across_chunks():
    body = mjpeg_body([JPEG], boundary="--frameboundary")
    parser = MjpegParser("--frameboundary")
    split = 6  # mid-delimiter
    frames = parser.feed(body[:split]) + parser.feed(body[split:])
    assert frames == [JPEG]


def test_closing_delimiter_ends_the_stream():
    parser = MjpegParser("bnd")
    frames = parser.feed(mjpeg_body([JPEG], boundary="bnd", close=True))
    assert frames == [JPEG]
    assert parser.closed is True
    # Anything after the close is epilogue, not frames.
    assert parser.feed(mjpeg_body([JPEG], boundary="bnd")) == []


def test_non_jpeg_parts_are_dropped():
    """An error body served mid-stream must not reach a viewer's <img>."""
    parser = MjpegParser("bnd")
    body = mjpeg_body([b"<html>camera is sad</html>", JPEG], boundary="bnd")
    assert parser.feed(body) == [JPEG]


def test_leading_preamble_is_skipped():
    parser = MjpegParser("bnd")
    body = b"ignored preamble\r\n" + mjpeg_body([JPEG], boundary="bnd")
    assert parser.feed(body) == [JPEG]


def test_buffer_is_capped():
    """A stream of junk must not grow in memory until the app is killed."""
    parser = MjpegParser("bnd", max_buffer_bytes=4096)
    with pytest.raises(MjpegError):
        for _ in range(10):
            parser.feed(b"n" * 1024)


def test_garbage_content_length_falls_back_to_delimiter_scan():
    parser = MjpegParser("bnd")
    body = (b"--bnd\r\nContent-Type: image/jpeg\r\nContent-Length: nonsense\r\n\r\n"
            + JPEG + b"\r\n--bnd\r\n")
    assert parser.feed(body) == [JPEG]
