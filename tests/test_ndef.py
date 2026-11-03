import pytest

from winnie_nfc import ndef


def test_build_text_wraps_the_message_in_a_tlv():
    data = ndef.build_text("hello", lang="en")
    # NDEF TLV: type 0x03, length, payload, terminator 0xFE
    assert data[0] == 0x03
    assert data[-1] == 0xFE
    assert data[1] == len(data) - 3


def test_text_round_trip():
    data = ndef.build_text("hello", lang="en")
    records = ndef.parse(data)
    assert records == [{"type": "text", "value": "hello"}]


def test_uri_round_trip_with_a_known_prefix():
    data = ndef.build_uri("https://pollen-robotics.com")
    # https:// is prefix 0x04, it must be abbreviated
    assert b"https://" not in data
    assert ndef.parse(data) == [{"type": "uri", "value": "https://pollen-robotics.com"}]


def test_uri_round_trip_without_a_known_prefix():
    data = ndef.build_uri("tftp://example")
    assert ndef.parse(data) == [{"type": "uri", "value": "tftp://example"}]


def test_accented_text_in_utf8():
    data = ndef.build_text("naïve café façade")
    assert ndef.parse(data) == [{"type": "text", "value": "naïve café façade"}]


def test_a_blank_tag_holds_no_record():
    # 0x03 0x00: empty NDEF TLV, then the terminator
    assert ndef.parse(bytes([0x03, 0x00, 0xFE])) == []


def test_an_uninitialised_area_raises():
    with pytest.raises(ndef.NdefError):
        ndef.parse(bytes(16))  # all zeros, no valid TLV


def test_an_inconsistent_tlv_length_raises():
    with pytest.raises(ndef.NdefError):
        ndef.parse(bytes([0x03, 0x50, 0x01, 0x02]))  # announces 80, gives 2


def test_a_long_text_switches_the_record_to_the_long_format():
    # An NTAG215 holds 504 bytes: a 300-character text is an ordinary case,
    # and it exceeds what the short format can encode.
    text = "a" * 300
    data = ndef.build_text(text)
    payload = ndef._extract_message(data)
    assert not (payload[0] & ndef.FLAG_SR), "the SR flag must drop"
    assert ndef.parse(data) == [{"type": "text", "value": text}]


def test_a_long_tlv_announces_its_length_on_two_bytes():
    data = ndef.build_text("a" * 400)
    assert data[0] == 0x03
    assert data[1] == 0xFF  # long format marker
    assert ((data[2] << 8) | data[3]) == len(data) - 5


def test_round_trip_around_the_255_byte_threshold():
    # 252 stays in the short format, 253 switches: the payload is 1 status byte
    # + 2 language code bytes + the text.
    for n in (250, 252, 253, 255, 256, 300):
        text = "a" * n
        assert ndef.parse(ndef.build_text(text)) == [{"type": "text", "value": text}], (
            f"failed at n={n}"
        )


def test_long_uri_round_trip():
    uri = "https://example.com/" + "x" * 400
    assert ndef.parse(ndef.build_uri(uri)) == [{"type": "uri", "value": uri}]


def test_the_longest_text_that_fits_on_an_ntag215():
    # 489 characters produce exactly 504 bytes, the tag's capacity.
    text = "a" * 489
    data = ndef.build_text(text)
    assert len(data) == 504
    assert ndef.parse(data) == [{"type": "text", "value": text}]


def test_empty_text_stays_readable():
    assert ndef.parse(ndef.build_text("")) == [{"type": "text", "value": ""}]


# --- robust decoding: what comes from phones and from other encoders ---


def _raw_record(header, type_name, payload, id_field=b""):
    """Assemble a record by hand, to build cases we never write ourselves."""
    body = bytes([header, len(type_name)])
    body += (
        bytes([len(payload)])
        if header & ndef.FLAG_SR
        else len(payload).to_bytes(4, "big")
    )
    if header & ndef.FLAG_IL:
        body += bytes([len(id_field)])
    body += type_name + id_field + payload
    return ndef._wrap_tlv(body)


def test_utf16_text_with_a_bom():
    payload = bytes([0x80 | 2]) + b"en" + "café".encode("utf-16")
    data = _raw_record(0xD1, b"T", payload)
    assert ndef.parse(data) == [{"type": "text", "value": "café"}]


def test_utf16_text_without_a_bom_is_read_as_big_endian():
    # The NFC Forum specification mandates big-endian in the absence of a BOM;
    # Python assumes the opposite, hence the explicit choice in _decode_text.
    payload = bytes([0x80 | 2]) + b"en" + "key".encode("utf-16-be")
    assert ndef.parse(_raw_record(0xD1, b"T", payload)) == [
        {"type": "text", "value": "key"}
    ]


def test_a_record_with_an_id_field_does_not_shift_the_rest():
    # The IL flag adds one ID-length byte: ignoring it shifted the type and
    # truncated the payload, with no error to say so.
    payload = bytes([0x02]) + b"en" + b"hello"
    data = _raw_record(0xD1 | ndef.FLAG_IL, b"T", payload, id_field=b"abc")
    assert ndef.parse(data) == [{"type": "text", "value": "hello"}]


def test_an_unknown_record_is_reported_rather_than_dropped():
    # A MIME payload: dropped in silence, it made the tag indistinguishable
    # from a blank one.
    data = _raw_record(0xD2, b"text/vcard", b"BEGIN:VCARD")
    records = ndef.parse(data)
    assert len(records) == 1
    assert records[0]["type"] == "other"
    assert records[0]["tnf"] == 0x02
    assert records[0]["type_name"] == "text/vcard"
    assert records[0]["payload"] == b"BEGIN:VCARD"


def test_type_t_outside_well_known_is_not_decoded_as_text():
    # "T" only means "text" in the Well Known name space. Under another TNF it
    # is a different type, and decoding it would invent content.
    data = _raw_record(0xD4, b"T", b"\x02enhello")  # TNF 4 = external type
    assert ndef.parse(data)[0]["type"] == "other"


def test_format_record():
    other = {
        "type": "other",
        "tnf": 2,
        "type_name": "text/vcard",
        "payload": bytes(range(20)),
    }
    line = ndef.format_record(other)
    assert "text/vcard" in line and "20 bytes" in line and "..." in line
    assert ndef.format_record({"type": "text", "value": "x"}) == "x"


def test_format_record_escapes_what_the_tag_could_inject():
    hostile = "\x1b]0;pwned\x07\x1b[2Jok\nfake line ‮abc"
    text = ndef.parse(ndef.build_text(hostile))[0]
    assert text["value"] == hostile, "parse must return the content untouched"
    line = ndef.format_record(text)
    assert line.isprintable()
    assert line == ("\\x1b]0;pwned\\x07\\x1b[2Jok\\nfake line \\u202eabc")

    other = {"type": "other", "tnf": 4, "type_name": "a\x1b[2Jb", "payload": b""}
    assert "\x1b" not in ndef.format_record(other)
    assert ndef.format_record({"type": "text", "value": "été �"}) == "été �"


def test_a_blank_tag_raises_absence_rather_than_malformation():
    # A service watching the antenna must be able to say "blank tag" without
    # confusing that case with damaged bytes.
    with pytest.raises(ndef.NdefAbsentError):
        ndef.parse(bytes(16))
    with pytest.raises(ndef.NdefError):  # the subclass stays catchable
        ndef.parse(bytes(16))
    with pytest.raises(ndef.NdefError) as exc:
        ndef.parse(bytes([0x03, 0x50, 0x01, 0x02]))
    assert not isinstance(exc.value, ndef.NdefAbsentError)


def test_a_non_ascii_language_code_is_a_usage_error():
    with pytest.raises(ndef.NdefError):
        ndef.build_text("hello", lang="é")
