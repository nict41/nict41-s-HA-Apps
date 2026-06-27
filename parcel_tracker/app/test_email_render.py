import email_render


def s(raw):
    return email_render.sanitize_email_html(raw)


def test_strips_script_tag_and_its_contents():
    out = s("<p>Hi</p><script>alert(1)</script><p>Bye</p>")
    assert "alert" not in out
    assert "<script" not in out
    assert "Hi" in out and "Bye" in out


def test_strips_style_tag_and_its_contents():
    out = s("<style>@import url(http://evil/x.css)</style><p>ok</p>")
    assert "import" not in out and "evil" not in out
    assert "ok" in out


def test_drops_event_handler_attributes():
    out = s('<img src="https://x/y.png" onerror="alert(1)" alt="a">')
    assert "onerror" not in out
    assert "alert" not in out
    # The tag and safe attributes survive.
    assert "src=" in out and 'alt="a"' in out


def test_drops_javascript_href_but_keeps_anchor_text():
    out = s('<a href="javascript:alert(1)">click</a>')
    assert "javascript:" not in out
    assert "click" in out


def test_keeps_safe_http_href_and_adds_hardened_rel():
    out = s('<a href="https://example.com/track">Track</a>')
    assert 'href="https://example.com/track"' in out
    assert "noopener" in out and "noreferrer" in out


def test_keeps_remote_image_src_for_csp_to_govern():
    out = s('<img src="https://cdn.example.com/pixel.png">')
    # Kept in the markup; whether it actually loads is decided by the CSP.
    assert 'src="https://cdn.example.com/pixel.png"' in out


def test_keeps_data_image_src():
    out = s('<img src="data:image/png;base64,AAAA">')
    assert "data:image/png;base64,AAAA" in out


def test_drops_data_html_src():
    out = s('<img src="data:text/html,<script>alert(1)</script>">')
    assert "data:text/html" not in out
    assert "alert" not in out


def test_strips_dangerous_style_value():
    out = s('<div style="width:expression(alert(1))">x</div>')
    assert "expression" not in out
    # The element itself stays, just without the poisoned style.
    assert ">x<" in out


def test_keeps_benign_inline_style():
    out = s('<div style="color:red;font-weight:bold">x</div>')
    assert "color:red" in out


def test_unknown_tag_is_unwrapped_but_content_survives():
    out = s("<marquee>scrolling</marquee>")
    assert "<marquee" not in out
    assert "scrolling" in out


def test_iframe_is_dropped_with_contents():
    out = s('<iframe src="https://evil"></iframe><p>safe</p>')
    assert "iframe" not in out and "evil" not in out
    assert "safe" in out


def test_text_is_html_escaped():
    out = s("<p>1 < 2 & 3 > 0</p>")
    assert "&lt;" in out and "&amp;" in out


def test_csp_blocks_remote_images_by_default():
    csp = email_render.content_security_policy(allow_remote_images=False)
    assert "default-src 'none'" in csp
    assert "img-src data:" in csp
    assert "https:" not in csp
    # No script source is ever permitted.
    assert "script-src" not in csp


def test_csp_allows_https_images_when_opted_in():
    csp = email_render.content_security_policy(allow_remote_images=True)
    assert "img-src data: https:" in csp
    assert "default-src 'none'" in csp


def test_render_document_wraps_body():
    doc = email_render.render_document("<p>hello</p>")
    assert doc.startswith("<!DOCTYPE html>")
    assert "<p>hello</p>" in doc
    assert "<body>" in doc


def test_text_fallback_escapes_and_preserves_whitespace():
    out = email_render.text_fallback_html("line1\n<tag> & stuff")
    assert "&lt;tag&gt;" in out
    assert "white-space:pre-wrap" in out
