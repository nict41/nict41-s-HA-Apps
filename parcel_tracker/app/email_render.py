"""Render a stored email's HTML safely for the dashboard's email viewer.

Email HTML is fully untrusted, hostile content, so it gets three independent
layers of defence before it's shown:

  1. `sanitize_email_html` strips it down to an allowlist of formatting tags
     and attributes here - no scripts, no event handlers, no dangerous URL
     schemes, no remote-resource-loading style tricks.
  2. The viewer route serves it with a strict Content-Security-Policy
     (`content_security_policy`) that allows no scripts at all and blocks
     every remote resource by default (so tracking pixels don't fire unless
     the user explicitly opts in to remote images).
  3. The dashboard embeds the result in a fully sandboxed <iframe> (no
     allow-scripts, no allow-same-origin), so even content that somehow slips
     past 1 and 2 runs in an opaque origin that can't script anything or
     reach the parent page.

A hand-written allowlist parser is used rather than a third-party sanitiser
on purpose: it adds no dependency to an Alpine/musl add-on image (where a
compiled package can fail to build), and the sandbox in layer 3 - not this
parser - is the actual security boundary. This keeps the parser conservative
and easy to audit: anything not explicitly allowed is dropped.
"""

import html as _html
import re
from html.parser import HTMLParser

# Tags whose entire subtree is discarded (tag *and* contents). Anything that
# can execute, load, or embed external content goes here.
_DROP_WITH_CONTENT = frozenset({
    "script", "style", "head", "title", "meta", "link", "noscript",
    "template", "object", "embed", "applet", "iframe", "frame", "frameset",
    "svg", "math", "base", "form", "input", "button", "textarea", "select",
    "option", "label", "fieldset", "legend", "audio", "video", "source",
    "track", "canvas", "map", "param",
})

# Tags that render with no closing tag of their own.
_VOID = frozenset({"br", "hr", "img", "col", "wbr", "area"})

# Tags kept (their attributes are still filtered). A tag not listed here and
# not in _DROP_WITH_CONTENT is unwrapped: its text/children survive but the
# tag itself is dropped, so unknown markup degrades to plain content.
_ALLOWED_TAGS = frozenset({
    "a", "abbr", "address", "article", "aside", "b", "bdi", "bdo",
    "blockquote", "br", "caption", "center", "cite", "code", "col",
    "colgroup", "dd", "del", "dfn", "div", "dl", "dt", "em", "figcaption",
    "figure", "font", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hgroup", "hr", "i", "img", "ins", "kbd", "li", "main", "mark", "nav",
    "ol", "p", "pre", "q", "s", "samp", "section", "small", "span", "strike",
    "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th",
    "thead", "time", "tr", "u", "ul", "var", "wbr",
})

_GLOBAL_ATTRS = frozenset({
    "style", "class", "id", "title", "dir", "align", "valign", "width",
    "height", "bgcolor", "color", "lang", "nowrap",
})

_TAG_ATTRS = {
    "a": frozenset({"href", "name"}),
    "img": frozenset({"src", "alt", "border", "hspace", "vspace"}),
    "td": frozenset({"colspan", "rowspan", "headers", "scope"}),
    "th": frozenset({"colspan", "rowspan", "headers", "scope"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "summary"}),
    "time": frozenset({"datetime"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "font": frozenset({"face", "size"}),
}

# Link/anchor targets we allow. Relative and scheme-relative ("//host") URLs
# are dropped: links are inert inside the sandbox anyway, and this avoids any
# ambiguity about what a bare path would resolve against.
_SAFE_HREF = re.compile(r"(?i)^(https?:|mailto:|tel:)")
_SAFE_IMG_REMOTE = re.compile(r"(?i)^https?://")
_SAFE_IMG_DATA = re.compile(r"(?i)^data:image/(png|jpe?g|gif|webp|bmp|svg\+xml);")
# Style values containing any of these are dropped wholesale - they're the
# vectors for script execution or remote loads from within CSS.
_BAD_STYLE = re.compile(
    r"(?i)(expression\s*\(|javascript:|vbscript:|behaviou?r\s*:|-moz-binding|@import|<)"
)


def _safe_href(value: str) -> bool:
    value = value.strip()
    return value.startswith("#") or bool(_SAFE_HREF.match(value))


def _safe_img_src(value: str) -> str | None:
    value = value.strip()
    if _SAFE_IMG_DATA.match(value) or _SAFE_IMG_REMOTE.match(value):
        return value
    return None


def _safe_style(value: str) -> str:
    # url(...) is intentionally left in place: any remote fetch it triggers is
    # already governed by the Content-Security-Policy (img-src), so it loads
    # only when the user has opted in to remote images.
    if _BAD_STYLE.search(value):
        return ""
    return value.strip()


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    # ---- dropped-subtree bookkeeping ----
    def _in_skip(self) -> bool:
        return self._skip_depth > 0

    def handle_starttag(self, tag, attrs):
        if self._in_skip():
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        rendered = self._render_attrs(tag, attrs)
        closer = "/" if tag in _VOID else ""
        self._out.append(f"<{tag}{rendered}{closer}>")

    def handle_startendtag(self, tag, attrs):
        if self._in_skip() or tag in _DROP_WITH_CONTENT or tag not in _ALLOWED_TAGS:
            return
        rendered = self._render_attrs(tag, attrs)
        self._out.append(f"<{tag}{rendered}/>")

    def handle_endtag(self, tag):
        if self._in_skip():
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _VOID or tag not in _ALLOWED_TAGS:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._in_skip():
            self._out.append(_html.escape(data, quote=False))

    def _render_attrs(self, tag: str, attrs) -> str:
        allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag, frozenset())
        parts: list[str] = []
        for raw_name, value in attrs:
            name = raw_name.lower()
            # Any on* handler, and anything outside the allowlist, is dropped.
            if name.startswith("on") or name not in allowed:
                continue
            if value is None:
                parts.append(f" {name}")
                continue
            if name == "href":
                if not _safe_href(value):
                    continue
            elif name == "src":
                checked = _safe_img_src(value)
                if checked is None:
                    continue
                value = checked
            elif name == "style":
                value = _safe_style(value)
                if not value:
                    continue
            parts.append(f' {name}="{_html.escape(value, quote=True)}"')
        # Links can't navigate inside the sandbox, but tag on a hardened rel
        # anyway as defence in depth.
        if tag == "a":
            parts.append(' rel="noopener noreferrer nofollow"')
        return "".join(parts)


def sanitize_email_html(raw: str) -> str:
    """Reduce untrusted email HTML to a safe formatting-only subset."""
    if not raw:
        return ""
    parser = _Sanitizer()
    parser.feed(raw)
    parser.close()
    return "".join(parser._out)


def content_security_policy(allow_remote_images: bool) -> str:
    """CSP for the email viewer document. No scripts at all; every remote
    resource blocked unless the viewer explicitly opted in to remote images
    (which still only relaxes images, nothing else)."""
    img_src = "img-src data:" + (" https:" if allow_remote_images else "")
    return "; ".join([
        "default-src 'none'",
        "style-src 'unsafe-inline'",
        img_src,
        "font-src data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self'",
    ])


# Readable defaults so a message designed for a white email client still reads
# correctly: the email's own inline styles layer on top of these.
_DOCUMENT_STYLE = (
    "body{margin:0;padding:16px;background:#ffffff;color:#1a1a1a;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    "font-size:14px;line-height:1.5;word-break:break-word;overflow-wrap:anywhere;}"
    "img{max-width:100%;height:auto;}table{max-width:100%;}a{color:#3047c0;}"
)


def render_document(body_html: str) -> str:
    """Wrap sanitised body HTML in a minimal, self-contained document."""
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<style>{_DOCUMENT_STYLE}</style></head><body>{body_html}</body></html>"
    )


def text_fallback_html(text: str) -> str:
    """Body HTML for a parcel whose source email was plain-text only."""
    return (
        "<pre style=\"white-space:pre-wrap;word-break:break-word;margin:0;"
        "font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px\">"
        + _html.escape(text or "")
        + "</pre>"
    )
