"""fonts.py: bundled families, user "file:" fonts, variable-font weights, caching
and the never-raise fallback.

Tests that need a real .ttf look for one of the usual DejaVu locations (or
$TD_TEST_FONT) and skip when none exists.
"""

import os
import shutil

import pytest

from conftest import pil, requires_pil

pytestmark = requires_pil

if pil:
    from PIL import ImageFont
    import fonts


def _test_font() -> str | None:
    env = os.environ.get("TD_TEST_FONT")
    candidates = [
        env,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/tmp/td/fonts/DejaVuSans.ttf",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


requires_ttf = pytest.mark.skipif(_test_font() is None, reason="no .ttf available in this environment")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """Every test gets a private fonts dir and user dir, and cleared caches."""
    monkeypatch.setattr(fonts, "FONTS_DIR", tmp_path / "bundled")
    monkeypatch.setattr(fonts, "_WARNED", set())
    fonts.set_user_dir(str(tmp_path / "userfonts"))
    fonts.load_font.cache_clear()
    fonts._load.cache_clear()
    fonts._default_font.cache_clear()
    yield
    fonts.set_user_dir(None)
    fonts.load_font.cache_clear()
    fonts._load.cache_clear()
    fonts._default_font.cache_clear()


def test_families_have_display_names_and_files():
    fams = fonts.FAMILIES
    assert fams["default"][0] is None
    for fid, (fname, name) in fams.items():
        if fid == "default":
            continue
        assert fname.endswith((".ttf", ".otf")), fid
        assert name and name != fid, fid          # a human name for the editor
    listed = fonts.bundled_families()
    assert listed[0] == {"id": "default", "name": "Default"}
    assert {"id": "bebas-neue", "name": "Bebas Neue"} in listed
    assert len(listed) == len(fams)


def test_default_family_is_todays_behaviour():
    a = fonts.load_font(20)
    b = fonts.load_font(20, family=None)
    c = fonts.load_font(20, family="default")
    assert a is b is c                            # same cache entry
    assert fonts.load_font(20, bold=True) is not a


def test_unknown_family_falls_back_with_one_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="fonts"):
        f1 = fonts.load_font(20, family="no-such-family")
        f2 = fonts.load_font(24, family="no-such-family")   # warned only once
    assert f1 is not None and f2 is not None                 # never raises
    assert f1 is fonts.load_font(20)                         # the default font
    warns = [r for r in caplog.records if "no-such-family" in r.getMessage()]
    assert len(warns) == 1


def test_missing_bundled_file_falls_back_with_one_warning(tmp_path, caplog):
    import logging
    (tmp_path / "bundled").mkdir()
    with caplog.at_level(logging.WARNING, logger="fonts"):
        f = fonts.load_font(20, family="bebas-neue")     # file not downloaded here
        f2 = fonts.load_font(22, family="bebas-neue")
    assert f is fonts.load_font(20)
    warns = [r for r in caplog.records if "BebasNeue-Regular.ttf" in r.getMessage()]
    assert len(warns) == 1


def test_bundled_family_lookup(tmp_path):
    src = _test_font()
    if src is None:
        pytest.skip("no .ttf available in this environment")
    (tmp_path / "bundled").mkdir()
    shutil.copy(src, tmp_path / "bundled" / "BebasNeue-Regular.ttf")
    f = fonts.load_font(20, family="bebas-neue")
    assert isinstance(f, ImageFont.FreeTypeFont)
    assert f.path.endswith("BebasNeue-Regular.ttf")
    assert fonts.family_file("bebas-neue") == "BebasNeue-Regular.ttf"
    assert fonts.family_display_name("bebas-neue") == "Bebas Neue"


@requires_ttf
def test_file_font_from_user_dir(tmp_path):
    src = _test_font()
    udir = tmp_path / "userfonts"
    udir.mkdir()
    shutil.copy(src, udir / "MyFont.ttf")
    fonts.set_user_dir(str(udir))
    f = fonts.load_font(20, family="file:MyFont.ttf")
    assert isinstance(f, ImageFont.FreeTypeFont)
    assert f.path.endswith("MyFont.ttf")
    # .otf names are fine too
    shutil.copy(src, udir / "Other.otf")
    assert fonts.load_font(20, family="file:Other.otf").path.endswith("Other.otf")


@requires_ttf
def test_file_font_missing_or_unsafe_falls_back(tmp_path, caplog):
    import logging
    udir = tmp_path / "userfonts"
    udir.mkdir()
    fonts.set_user_dir(str(udir))
    with caplog.at_level(logging.WARNING, logger="fonts"):
        assert fonts.load_font(20, family="file:Nope.ttf") is fonts.load_font(20)
        assert fonts.load_font(20, family="file:../screens.yaml") is fonts.load_font(20)
        assert fonts.load_font(20, family="file:evil.png") is fonts.load_font(20)
    assert fonts.user_font_path("../x.ttf") is None
    assert fonts.safe_font_name("a b.ttf") == "a b.ttf"
    assert fonts.safe_font_name(".hidden.ttf") is None
    assert fonts.safe_font_name("x.txt") is None


def test_lru_cache_returns_same_object_and_is_bounded():
    a = fonts.load_font(20, family="default")
    b = fonts.load_font(20, family="default")
    assert a is b
    info = fonts.load_font.cache_info()
    assert info.maxsize == 64
    assert fonts._load.cache_info().maxsize == 256
    for size in range(1, 80):            # more than maxsize -> evictions, still works
        fonts.load_font(size)
    assert fonts.load_font(20) is not None


@requires_ttf
def test_variable_font_weight(tmp_path):
    """A font with a wght axis gets 700 for bold and 400 for regular; a static
    font (DejaVu) ignores bold. No variable font is available offline, so this
    checks the guard on a static font plus the setter path with the axis check
    forced on."""
    src = _test_font()
    f = ImageFont.truetype(src, 20)
    assert fonts._has_wght_axis(f) is False          # DejaVu is static
    assert fonts._apply_weight(f, True) is f         # no fake bold
    g = ImageFont.truetype(src, 20)
    orig = fonts._has_wght_axis
    fonts._has_wght_axis = lambda font: True         # pretend it is variable
    try:
        assert fonts._apply_weight(g, True) is g     # setter path runs, no raise
        assert fonts._apply_weight(g, False) is g
    finally:
        fonts._has_wght_axis = orig
