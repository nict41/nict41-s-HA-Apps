import carriers


def test_aliexpress_label_parser_extracts_tracking_number():
    sender = "noreply@aliexpress.com"
    subject = "Your order has shipped!"
    body = "Good news! Tracking Number: LP00123456789CN Carrier: Cainiao Standard"
    candidates = carriers.detect_candidates(sender, subject, body)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.tracking_number == "LP00123456789CN"
    assert candidate.confidence == 0.92
    assert "Cainiao" in candidate.carrier_name


def test_aliexpress_label_parser_falls_back_to_retailer_name_without_carrier_label():
    sender = "shipping@aliexpress.com"
    subject = "Item dispatched"
    body = "Tracking Number: RR123456785CN. Thanks for shopping with us."
    candidates = carriers.detect_candidates(sender, subject, body)
    assert len(candidates) == 1
    assert candidates[0].carrier_name == "AliExpress"


def test_ebay_label_parser_extracts_tracking_number():
    sender = "ebay@ebay.com"
    subject = "Your item has shipped"
    body = "Tracking number: 1Z999AA10123456784 Carrier: UPS"
    candidates = carriers.detect_candidates(sender, subject, body)
    assert len(candidates) == 1
    assert candidates[0].tracking_number == "1Z999AA10123456784"
    assert candidates[0].carrier_name == "UPS"
    assert candidates[0].confidence == 0.92


def test_generic_ups_pattern_detected_with_shipping_context():
    sender = "alerts@somerandomstore.com"
    subject = "Your package is on its way"
    body = "Your tracking number is 1Z999AA10123456784, courier UPS."
    candidates = carriers.detect_candidates(sender, subject, body)
    numbers = {c.tracking_number for c in candidates}
    assert "1Z999AA10123456784" in numbers
    ups_candidate = next(c for c in candidates if c.tracking_number == "1Z999AA10123456784")
    assert ups_candidate.confidence >= carriers.CONFIRM_THRESHOLD


def test_no_shipping_context_returns_no_candidates():
    sender = "newsletter@somerandomstore.com"
    subject = "50% off everything this weekend"
    body = "Check out our sale! Item code 1234567890123 is a bestseller."
    candidates = carriers.detect_candidates(sender, subject, body)
    assert candidates == []


def test_low_confidence_generic_numeric_lands_below_confirm_threshold():
    sender = "alerts@somerandomstore.com"
    subject = "Your parcel is on its way"
    body = "Reference: 4456778899"
    candidates = carriers.detect_candidates(sender, subject, body)
    assert candidates
    assert all(c.confidence < carriers.CONFIRM_THRESHOLD for c in candidates)


def test_upu_s10_format_maps_to_country_carrier():
    sender = "alerts@somerandomstore.com"
    subject = "Shipment dispatched"
    body = "Your parcel tracking code is RR123456785CN"
    candidates = carriers.detect_candidates(sender, subject, body)
    candidate = next(c for c in candidates if c.tracking_number == "RR123456785CN")
    assert candidate.carrier_name == "China Post / ePacket"


def test_extra_trusted_domain_gets_label_parser_treatment():
    sender = "orders@myniceretailer.com"
    subject = "Shipped!"
    body = "Tracking Number: ABC123456789"
    candidates = carriers.detect_candidates(
        sender, subject, body, extra_trusted_domains=frozenset({"myniceretailer.com"})
    )
    assert len(candidates) == 1
    assert candidates[0].confidence == 0.92


def test_strip_html_removes_tags_and_decodes_entities():
    html = "<html><body><p>Tracking&nbsp;Number:&amp;nbsp;<b>LP123456789CN</b></p></body></html>"
    text = carriers.strip_html(html)
    assert "<" not in text
    assert "LP123456789CN" in text


def test_get_tracking_url_uses_known_template():
    url = carriers.get_tracking_url("UPS", "1Z999AA10123456784")
    assert "1Z999AA10123456784" in url
    assert "ups.com" in url


def test_get_tracking_url_falls_back_to_17track_for_unknown_carrier():
    url = carriers.get_tracking_url("Some Obscure Regional Carrier", "ABC123")
    assert "17track.net" in url
    assert "ABC123" in url
