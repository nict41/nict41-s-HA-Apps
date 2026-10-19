import base64
import re
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

import thumbnail

# A 10 mm cube: 12 triangles, outward winding.
V = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 10), (10, 0, 10), (10, 10, 10), (0, 10, 10)]
F = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
     (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def binary_stl(path: Path) -> Path:
    data = b"\0" * 80 + struct.pack("<I", len(F))
    for f in F:
        data += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *V[i]) for i in f) + b"\0\0"
    path.write_bytes(data)
    return path


def ascii_stl(path: Path) -> Path:
    out = ["solid cube"]
    for f in F:
        out += [" facet normal 0 0 0", "  outer loop"]
        out += [f"   vertex {V[i][0]} {V[i][1]} {V[i][2]}" for i in f]
        out += ["  endloop", " endfacet"]
    path.write_text("\n".join(out + ["endsolid cube"]))
    return path


def three_mf(path: Path) -> Path:
    """The layout Orca-family slicers write: the build item points at an
    object made of components, whose meshes live in a separate model file."""
    ns = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" ' \
         'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in V)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F)
    part = f'<model {ns}><resources><object id="1" type="model"><mesh><vertices>{verts}</vertices>' \
           f'<triangles>{tris}</triangles></mesh></object></resources></model>'
    root = f'<model {ns}><resources><object id="2" type="model"><components>' \
           '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>' \
           '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 20 0 0"/>' \
           '</components></object></resources>' \
           '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 100 0"/></build></model>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", root)
        zf.writestr("3D/Objects/object_1.model", part)
    return path


def decode_png(png: bytes) -> tuple[int, int, list[bytes]]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(png):
        n = struct.unpack(">I", png[pos:pos + 4])[0]
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + data)
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", data[:10])
            assert (depth, ctype) == (8, 6)
        elif kind == b"IDAT":
            idat += data
    raw = zlib.decompress(idat)
    stride = w * 4 + 1
    rows = [raw[i * stride + 1:(i + 1) * stride] for i in range(h)]
    assert all(raw[i * stride] == 0 for i in range(h))  # filter: none
    return w, h, rows


def test_stl_binary_and_ascii_load_the_same_triangles(tmp_path):
    a = thumbnail.load_triangles(binary_stl(tmp_path / "b.stl"))
    b = thumbnail.load_triangles(ascii_stl(tmp_path / "a.stl"))
    assert len(a) == len(b) == 12
    assert a == b  # small integers survive float32 exactly


def test_3mf_follows_components_and_transforms(tmp_path):
    tris = thumbnail.load_triangles(three_mf(tmp_path / "m.3mf"))
    assert len(tris) == 24
    xs = [v[0] for t in tris for v in t]
    # Two cubes, the second shifted 20 mm, both then moved 100 mm by the build item.
    assert (min(xs), max(xs)) == (100, 130)


def test_unsupported_model_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not an STL or 3MF"):
        thumbnail.load_triangles(tmp_path / "x.obj")


def test_render_is_a_transparent_png_with_the_model_in_the_middle(tmp_path):
    png = thumbnail.render(thumbnail.load_triangles(binary_stl(tmp_path / "c.stl")), "#FF0000")
    w, h, rows = decode_png(png)
    assert (w, h) == (144, 144)
    assert rows[0][3] == 0  # corner fully transparent
    centre = rows[72][72 * 4:72 * 4 + 4]
    assert centre[3] == 255
    assert centre[0] > centre[1] and centre[0] > centre[2]  # red filament renders red


def test_black_filament_still_shows_shape(tmp_path):
    png = thumbnail.render(thumbnail.load_triangles(binary_stl(tmp_path / "c.stl")), "#000000")
    _, _, rows = decode_png(png)
    shades = {rows[y][x * 4] for y in range(30, 114, 6) for x in range(30, 114, 6) if rows[y][x * 4 + 3] == 255}
    assert len(shades) > 1 and max(shades) > 20


def test_empty_model_is_an_error():
    with pytest.raises(ValueError):
        thumbnail.render([])


HEADER = "; HEADER_BLOCK_START\n; total layer number: 1\n; HEADER_BLOCK_END\n\n; filament: 1\n; HEADER_BLOCK_END\n\n" \
         "; external perimeters extrusion width = 0.42mm\n\n; EXECUTABLE_BLOCK_START\nG1 X1\n"


def test_embed_writes_the_gui_block_after_the_header(tmp_path):
    gcode = tmp_path / "a.gcode"
    gcode.write_text(HEADER)
    png = b"\x89PNG" + bytes(range(256)) * 3
    assert thumbnail.embed(gcode, png) is True
    text = gcode.read_text()
    assert text.startswith(HEADER.split("; external")[0][:-1] + "\n\n\n; THUMBNAIL_BLOCK_START\n\n;\n; thumbnail begin 144x144 ")
    m = re.search(r"; thumbnail begin 144x144 (\d+)\n(.*?)\n; thumbnail end\n; THUMBNAIL_BLOCK_END\n\n; external", text, re.S)
    assert m
    lines = m.group(2).split("\n")
    assert all(len(line) == 80 for line in lines[:-1])
    encoded = "".join(line[2:] for line in lines)
    assert int(m.group(1)) == len(encoded)
    assert base64.b64decode(encoded) == png
    assert text.endswith("; EXECUTABLE_BLOCK_START\nG1 X1\n")


def test_embed_leaves_a_file_that_already_has_a_thumbnail(tmp_path):
    gcode = tmp_path / "a.gcode"
    gcode.write_text(HEADER.replace("; external", "; thumbnail begin 1x1 4\n; AAAA\n; thumbnail end\n; external"))
    before = gcode.read_bytes()
    assert thumbnail.embed(gcode, b"png") is False
    assert gcode.read_bytes() == before


@pytest.mark.parametrize("azimuth", [0, 45, 135, -135, 200])
@pytest.mark.parametrize("elevation", [10, 45, 80])
def test_camera_basis_is_right_handed_and_looks_from_the_front(azimuth, elevation):
    import math
    right, up, towards = thumbnail.camera(math.radians(azimuth), math.radians(elevation))
    cross = (right[1] * up[2] - right[2] * up[1], right[2] * up[0] - right[0] * up[2], right[0] * up[1] - right[1] * up[0])
    assert cross == pytest.approx(towards)
    assert towards[2] > 0  # the camera is above the bed
    assert up[2] > 0  # and the model's up is up on screen


def test_depth_keeps_the_near_surface_and_front_faces_survive_culling():
    import math
    basis = thumbnail.camera(0, math.radians(30))
    # Unrotated, the camera is in front (-y) of the model: nearer points have
    # greater depth.
    assert thumbnail.view(basis, (0, -10, 0))[2] > thumbnail.view(basis, (0, 10, 0))[2]
    # A cube's front face (outward normal -y), wound outward, must face the
    # camera in screen space, or render() culls it.
    a, b, c = (thumbnail.view(basis, V[i]) for i in F[4])  # (0, 1, 5): the y=0 face
    nz = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    assert nz > 0


def test_a_closed_box_renders_its_lit_top_not_its_inside(tmp_path):
    """Seen from above, a box shows its top face, which faces the light; the
    inside of its floor - what a mirrored camera shows - is much darker."""
    _, _, rows = decode_png(thumbnail.render(thumbnail.load_triangles(binary_stl(tmp_path / "c.stl")), "#808080"))
    top = [rows[y][x * 4] for y in range(20, 50) for x in range(60, 84) if rows[y][x * 4 + 3] == 255]
    assert top and sum(top) / len(top) > 120
