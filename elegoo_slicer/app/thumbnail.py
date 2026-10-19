"""Plate thumbnails for G-code the slicer's command line leaves without one.

ElegooSlicer 1.5.3.5 cannot render thumbnails from its command line on
Linux: it asks GLFW for an OpenGL context of version glfwGetVersion() - the
GLFW library's own version, 3.4 - and OpenGL 3.4 does not exist, so context
creation fails every time and the G-code goes out with no preview. The
printer's screen and Home Assistant's cover image both show that preview.

So the model is rendered here instead: an orthographic three-quarter view,
flat-shaded in the filament colour, on a transparent background, at the size
and in the block format the GUI writes. Standard library only - a z-buffer
rasteriser and a PNG encoder are small, and a few hundred thousand triangles
at this size take seconds.
"""

from __future__ import annotations

import base64
import math
import re
import struct
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

Triangle = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]

SIZE = 144
SUPERSAMPLE = 3
# Seen from the front-left and above, like the GUI's plate thumbnail.
AZIMUTH = math.radians(135)
ELEVATION = math.radians(45)
MARGIN = 0.17


# ── Models ──────────────────────────────────────────────────────────────────


def load_triangles(path: Path) -> list[Triangle]:
    suffix = path.suffix.lower()
    if suffix == ".stl":
        return _load_stl(path.read_bytes())
    if suffix == ".3mf":
        return _load_3mf(path)
    raise ValueError(f"cannot render {path.name}: not an STL or 3MF")


def _load_stl(data: bytes) -> list[Triangle]:
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * count == len(data):
            tris = []
            for i in range(count):
                v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
                tris.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
            return tris
    # ASCII: every "vertex x y z", three to a facet.
    nums = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
    verts = [(float(x), float(y), float(z)) for x, y, z in nums]
    return [tuple(verts[i:i + 3]) for i in range(0, len(verts) - 2, 3)]  # type: ignore[misc]


_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_PROD = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
Matrix = tuple[float, ...]  # 3MF's 3x4, row-major: m00 m01 m02 m10 ... m32
IDENTITY: Matrix = (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)


def _matrix(text: str | None) -> Matrix:
    if not text:
        return IDENTITY
    vals = tuple(float(v) for v in text.split())
    return vals if len(vals) == 12 else IDENTITY


def _compose(outer: Matrix, inner: Matrix) -> Matrix:
    """inner applied first, then outer (3MF row-vector convention)."""
    a, b = inner, outer
    rot = [[a[r * 3 + c] for c in range(3)] for r in range(3)]
    rot_b = [[b[r * 3 + c] for c in range(3)] for r in range(3)]
    out = [sum(rot[r][k] * rot_b[k][c] for k in range(3)) for r in range(3) for c in range(3)]
    t = [sum(a[9 + k] * rot_b[k][c] for k in range(3)) + b[9 + c] for c in range(3)]
    return tuple(out + t)


def _apply(m: Matrix, v: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = v
    return (
        x * m[0] + y * m[3] + z * m[6] + m[9],
        x * m[1] + y * m[4] + z * m[7] + m[10],
        x * m[2] + y * m[5] + z * m[8] + m[11],
    )


def _load_3mf(path: Path) -> list[Triangle]:
    with zipfile.ZipFile(path) as zf:
        models = {
            "/" + n: ElementTree.fromstring(zf.read(n))
            for n in zf.namelist() if n.lower().endswith(".model")
        }
    root_name = next((n for n in models if n.lower() == "/3d/3dmodel.model"), None)
    if root_name is None:
        raise ValueError(f"{path.name} has no 3D/3dmodel.model")

    objects: dict[tuple[str, str], ElementTree.Element] = {}
    for name, tree in models.items():
        for obj in tree.iter(f"{_NS}object"):
            objects[(name, obj.get("id", ""))] = obj

    tris: list[Triangle] = []

    def emit(model: str, obj_id: str, m: Matrix, depth: int) -> None:
        obj = objects.get((model, obj_id))
        if obj is None or depth > 16:
            return
        mesh = obj.find(f"{_NS}mesh")
        if mesh is not None:
            verts = [
                _apply(m, (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))))
                for v in mesh.iter(f"{_NS}vertex")
            ]
            for t in mesh.iter(f"{_NS}triangle"):
                try:
                    tris.append((verts[int(t.get("v1"))], verts[int(t.get("v2"))], verts[int(t.get("v3"))]))
                except (IndexError, TypeError, ValueError):
                    continue
        comps = obj.find(f"{_NS}components")
        if comps is not None:
            for comp in comps.iter(f"{_NS}component"):
                target = comp.get(f"{_PROD}path") or model
                emit(target, comp.get("objectid", ""), _compose(m, _matrix(comp.get("transform"))), depth + 1)

    build = models[root_name].find(f"{_NS}build")
    for item in build.iter(f"{_NS}item") if build is not None else ():
        target = item.get(f"{_PROD}path") or root_name
        emit(target, item.get("objectid", ""), _matrix(item.get("transform")), 0)
    return tris


# ── Rendering ───────────────────────────────────────────────────────────────


def _parse_colour(colour: str) -> tuple[float, float, float]:
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?", colour.strip())
    if not m:
        return (0.6, 0.6, 0.6)
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


Basis = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


def camera(azimuth: float, elevation: float) -> Basis:
    """Screen axes (right, up, towards the viewer) in model coordinates.

    The model is turned by `azimuth` about Z and seen from in front (-y) and
    `elevation` above. The basis is right-handed - right x up = towards - which
    the back-face test and the depth test in render() both rely on. Mirroring
    one axis flips every triangle's winding: the faces towards the camera get
    culled and the model's far inside walls are what is left to see.
    """
    ca, sa = math.cos(azimuth), math.sin(azimuth)
    ce, se = math.cos(elevation), math.sin(elevation)
    # Rows: the model's x, y, z rotated about Z, then dotted with each axis.
    right = (ca, -sa, 0.0)
    up = (sa * se, ca * se, ce)
    towards = (-sa * ce, -ca * ce, se)
    return right, up, towards


def view(basis: Basis, p: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(axis[0] * p[0] + axis[1] * p[1] + axis[2] * p[2] for axis in basis)  # type: ignore[return-value]


def render(tris: list[Triangle], colour: str = "#808080", size: int = SIZE) -> bytes:
    """Render to an RGBA PNG."""
    if not tris:
        raise ValueError("model has no triangles")
    basis = camera(AZIMUTH, ELEVATION)
    projected = [(view(basis, a), view(basis, b), view(basis, c)) for a, b, c in tris]
    xs = [v[0] for t in projected for v in t]
    ys = [v[1] for t in projected for v in t]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    px = size * SUPERSAMPLE
    scale = px * (1 - 2 * MARGIN) / span
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2

    depth = [-math.inf] * (px * px)
    shade = [0.0] * (px * px)
    # Light from over the viewer's left shoulder.
    lx, ly, lz = -0.35, 0.55, 0.76
    norm = math.sqrt(lx * lx + ly * ly + lz * lz)
    lx, ly, lz = lx / norm, ly / norm, lz / norm

    half = px / 2
    for (ax, ay, az), (bx, by, bz), (qx, qy, qz) in projected:
        ux, uy, uz = bx - ax, by - ay, bz - az
        wx, wy, wz = qx - ax, qy - ay, qz - az
        nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
        if nz <= 0:
            continue  # facing away
        nl = math.sqrt(nx * nx + ny * ny + nz * nz)
        light = max(0.0, (nx * lx + ny * ly + nz * lz) / nl)
        # Screen space, y down.
        x0, y0 = half + (ax - cx) * scale, half - (ay - cy) * scale
        x1, y1 = half + (bx - cx) * scale, half - (by - cy) * scale
        x2, y2 = half + (qx - cx) * scale, half - (qy - cy) * scale
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if area == 0:
            continue
        min_x = max(0, int(min(x0, x1, x2)))
        max_x = min(px - 1, int(max(x0, x1, x2)) + 1)
        min_y = max(0, int(min(y0, y1, y2)))
        max_y = min(px - 1, int(max(y0, y1, y2)) + 1)
        inv = 1 / area
        for yy in range(min_y, max_y + 1):
            sy = yy + 0.5
            row = yy * px
            for xx in range(min_x, max_x + 1):
                sx = xx + 0.5
                w0 = ((x1 - sx) * (y2 - sy) - (x2 - sx) * (y1 - sy)) * inv
                w1 = ((x2 - sx) * (y0 - sy) - (x0 - sx) * (y2 - sy)) * inv
                w2 = 1 - w0 - w1
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                z = w0 * az + w1 * bz + w2 * qz
                if z > depth[row + xx]:
                    depth[row + xx] = z
                    shade[row + xx] = light

    base = _parse_colour(colour)
    # Dark filament still needs visible form, so light lifts every colour a
    # little towards white rather than only scaling it.
    ss = SUPERSAMPLE
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter: none
        for x in range(size):
            r = g = b = a = 0.0
            for dy in range(ss):
                off = (y * ss + dy) * px + x * ss
                for dx in range(ss):
                    if depth[off + dx] == -math.inf:
                        continue
                    lit = shade[off + dx]
                    k = 0.35 + 0.65 * lit
                    lift = 0.10 + 0.22 * lit * lit
                    r += min(1.0, base[0] * k + lift)
                    g += min(1.0, base[1] * k + lift)
                    b += min(1.0, base[2] * k + lift)
                    a += 1
            if a:
                row += bytes((round(255 * r / a), round(255 * g / a), round(255 * b / a), round(255 * a / (ss * ss))))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    return _png(size, size, b"".join(rows))


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


# ── G-code ──────────────────────────────────────────────────────────────────


def thumbnail_block(png: bytes, size: int = SIZE) -> str:
    """The block exactly as the GUI writes it."""
    encoded = base64.b64encode(png).decode()
    lines = [encoded[i:i + 78] for i in range(0, len(encoded), 78)]
    body = "\n".join(f"; {line}" for line in lines)
    return (
        "\n\n\n; THUMBNAIL_BLOCK_START\n\n;\n"
        f"; thumbnail begin {size}x{size} {len(encoded)}\n{body}\n; thumbnail end\n"
        "; THUMBNAIL_BLOCK_END\n"
    )


def embed(gcode: Path, png: bytes) -> bool:
    """Insert the thumbnail after the header, unless the file already has one.

    The file is rewritten through a temporary copy so a failure part-way never
    leaves a truncated G-code behind.
    """
    with gcode.open("rb") as fh:
        head = fh.read(64 * 1024)
    if b"; thumbnail begin" in head:
        return False
    text = head.decode("utf-8", errors="replace")
    end = text.find("; EXECUTABLE_BLOCK_START")
    marker = "; HEADER_BLOCK_END\n"
    at = text.rfind(marker, 0, end if end >= 0 else len(text))
    if at < 0:
        return False
    cut = len(text[: at + len(marker)].encode("utf-8"))
    tmp = gcode.with_suffix(gcode.suffix + ".tmp")
    with gcode.open("rb") as src, tmp.open("wb") as dst:
        dst.write(src.read(cut))
        dst.write(thumbnail_block(png).encode())
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
    tmp.replace(gcode)
    return True
