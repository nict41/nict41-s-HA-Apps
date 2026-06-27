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


def test_strip_html_decodes_all_entities_and_drops_zero_width_padding():
    # The marketing-preheader case: a hidden line padded with &zwnj; / &#39;
    # must not leave literal "&...;" tokens or stray zero-width characters.
    html = "<span>We&#39;re getting your order ready &zwnj; &zwnj; &zwnj;</span>"
    text = carriers.strip_html(html)
    assert "&zwnj;" not in text
    assert "&#39;" not in text
    assert "‌" not in text
    assert "We're getting your order ready" in text


def test_get_tracking_url_points_at_track123():
    url = carriers.get_tracking_url("1Z999AA10123456784")
    assert "1Z999AA10123456784" in url
    assert "track123.com" in url


def test_retailer_domain_matches_notification_subdomain():
    # Real AliExpress shipping updates come from a subdomain, not the bare
    # domain - this must still get the high-confidence label parser.
    sender = "AliExpress <transaction@notice.aliexpress.com>"
    subject = "Package JJD0002234785196396: left the departure region"
    body = "Tracking Number: JJD0002234785196396."
    candidates = carriers.detect_candidates(sender, subject, body)
    assert len(candidates) == 1
    assert candidates[0].confidence == 0.92


def test_aliexpress_jjd_tracking_number_detected_without_explicit_label():
    sender = "AliExpress <transaction@notice.aliexpress.com>"
    subject = "Package JJD0002234785196396: left the departure region"
    body = (
        "Your package JJD0002234785196396 has left the place of departure "
        "and is now in transit to the destination country/region. "
        "Track delivery"
    )
    candidates = carriers.detect_candidates(sender, subject, body)
    numbers = {c.tracking_number for c in candidates}
    assert "JJD0002234785196396" in numbers
    candidate = next(c for c in candidates if c.tracking_number == "JJD0002234785196396")
    assert "Cainiao" in candidate.carrier_name
    assert candidate.confidence >= carriers.CONFIRM_THRESHOLD


def test_ebay_item_id_and_order_number_are_not_picked_up_as_tracking_numbers():
    # eBay order-confirmation emails print an Item ID and Order number before
    # a tracking number even exists - a bare 12-digit Item ID otherwise
    # collides with the generic FedEx-shaped pattern.
    sender = "eBay <ebay@ebay.com>"
    subject = "Order update: OXVA XLIM PODS KIT V3 Pod..."
    body = (
        "Hi Nicholas, keep track of your order. "
        "Item ID: 358665784843 "
        "Order number: 06-14764-13438 "
        "Seller: vapepoint "
        "Your order will be dispatched to: 34 Owens Quay"
    )
    candidates = carriers.detect_candidates(sender, subject, body)
    numbers = {c.tracking_number for c in candidates}
    assert "358665784843" not in numbers


def test_ebay_real_tracking_number_still_detected_alongside_item_id():
    sender = "eBay <ebay@ebay.com>"
    subject = "Order update: OXVA XLIM PODS KIT V3 Pod..."
    body = (
        "Hi Nicholas, keep track of your order. "
        "Tracking number: 32072900700074E3F1818 "
        "Item ID: 358665784843 "
        "Order number: 06-14764-13438 "
        "Seller: vapepoint"
    )
    candidates = carriers.detect_candidates(sender, subject, body)
    numbers = {c.tracking_number for c in candidates}
    assert numbers == {"32072900700074E3F1818"}
