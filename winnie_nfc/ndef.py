"""NDEF encoding and decoding.

Encoding is limited to text and URI records, the only two we ever write.
Decoding, on the other hand, also returns the records it cannot interpret, in
a raw form: see parse.

A pure module: no hardware dependency, fully testable without a board.
"""

TLV_NDEF = 0x03
TLV_TERMINATOR = 0xFE

TNF_WELL_KNOWN = 0x01
RTD_TEXT = b"T"
RTD_URI = b"U"

FLAG_MB = 0x80  # first record of the message
FLAG_ME = 0x40  # last record
FLAG_CF = 0x20  # record split into chunks (not handled)
FLAG_SR = 0x10  # short format: length on 1 byte
FLAG_IL = 0x08  # an ID field is present
TNF_MASK = 0x07

# Status byte of a text record: bit 7 = encoding, bits 5..0 = length of the
# language code.
STATUS_UTF16 = 0x80
STATUS_LANG_MASK = 0x3F

# NFC Forum abbreviation table: the index replaces the string in the payload.
URI_PREFIXES = (
    "",
    "http://www.",
    "https://www.",
    "http://",
    "https://",
    "tel:",
    "mailto:",
    "ftp://anonymous:anonymous@",
    "ftp://ftp.",
    "ftps://",
    "sftp://",
    "smb://",
    "nfs://",
    "ftp://",
    "dav://",
    "news:",
    "telnet://",
    "imap:",
    "rtsp://",
    "urn:",
    "pop:",
    "sip:",
    "sips:",
    "tftp:",
    "btspp://",
    "btl2cap://",
    "btgoep://",
    "tcpobex://",
    "irdaobex://",
    "file://",
    "urn:epc:id:",
    "urn:epc:tag:",
    "urn:epc:pat:",
    "urn:epc:raw:",
    "urn:epc:",
    "urn:nfc:",
)


class NdefError(Exception):
    """NDEF content is missing or malformed."""


class NdefAbsentError(NdefError):
    """No NDEF message on the tag: blank or not formatted.

    Distinct from malformed content: a tag out of the factory is nothing
    unusual, and a caller watching the antenna must be able to report "blank
    tag" without confusing that case with damaged bytes.
    """


def _wrap_tlv(payload):
    """Wrap an NDEF message in a TLV, with its terminator."""
    if len(payload) > 0xFE:
        # Long format: 0xFF then the length on 2 big-endian bytes.
        header = bytes(
            [TLV_NDEF, 0xFF, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]
        )
    else:
        header = bytes([TLV_NDEF, len(payload)])
    return header + payload + bytes([TLV_TERMINATOR])


def _build_record(rtd, payload):
    """Build a single record (so both the first and the last one).

    Beyond 255 payload bytes the short format (the SR flag) can no longer
    encode the length on a single byte: we switch to the long format, where it
    takes 4 big-endian bytes. Since an NTAG215 offers 504 user bytes, this case
    is ordinary rather than marginal — a 300-character text is enough to
    trigger it.

    The two format thresholds are independent: the TLV switches to the long
    format beyond 254 bytes (see _wrap_tlv), the record beyond 255. A message
    can therefore have a long TLV and a short record.
    """
    header = FLAG_MB | FLAG_ME | TNF_WELL_KNOWN
    if len(payload) <= 0xFF:
        return bytes([header | FLAG_SR, len(rtd), len(payload)]) + rtd + payload
    return bytes([header, len(rtd)]) + len(payload).to_bytes(4, "big") + rtd + payload


def build_text(text, lang="en"):
    """Encode `text` as an NDEF text record, wrapped in its TLV.

    `lang` is the language code of the content being written, not a setting of
    this library: it ends up in the record, and a reader may use it to pick a
    translation.
    """
    try:
        lang_bytes = lang.encode("ascii")
    except UnicodeEncodeError:
        raise NdefError(f"language code must be ASCII: {lang}") from None
    if len(lang_bytes) > 0x3F:
        raise NdefError(f"language code too long: {lang}")
    # Status byte: bit 7 clear = UTF-8, bits 5..0 = length of the language code.
    payload = bytes([len(lang_bytes)]) + lang_bytes + text.encode("utf-8")
    return _wrap_tlv(_build_record(RTD_TEXT, payload))


def build_uri(uri):
    """Abbreviate the longest possible prefix, to save bytes."""
    index, rest = 0, uri
    for i, prefix in enumerate(URI_PREFIXES):
        if prefix and uri.startswith(prefix) and len(prefix) > len(URI_PREFIXES[index]):
            index, rest = i, uri[len(prefix) :]
    payload = bytes([index]) + rest.encode("utf-8")
    return _wrap_tlv(_build_record(RTD_URI, payload))


def _extract_message(data):
    """Find the NDEF TLV and return its payload."""
    i = 0
    while i < len(data):
        tlv = data[i]
        if tlv == TLV_TERMINATOR:
            raise NdefAbsentError("terminator reached without an NDEF TLV")
        if tlv == 0x00:  # NULL TLV, to be skipped
            i += 1
            continue
        if i + 1 >= len(data):
            raise NdefError("truncated TLV")
        length = data[i + 1]
        if length == 0xFF:
            if i + 3 >= len(data):
                raise NdefError("truncated long TLV")
            length = (data[i + 2] << 8) | data[i + 3]
            start = i + 4
        else:
            start = i + 2
        if start + length > len(data):
            raise NdefError(
                f"the TLV announces {length} bytes, {len(data) - start} available"
            )
        if tlv == TLV_NDEF:
            return data[start : start + length]
        i = start + length
    raise NdefAbsentError("no NDEF TLV found: blank or unformatted tag")


def _decode_text(body):
    """Decode a text record payload: status, language code, text.

    The status byte carries the encoding in its bit 7. Ignoring that bit
    renders a UTF-16 text (as some Android phones write) as gibberish, without
    any error.

    The NFC Forum specification allows UTF-16 with or without a BOM, and
    mandates big-endian in its absence. Python assumes little-endian without a
    BOM, hence the explicit choice of utf-16-be.
    """
    status = body[0]
    n = status & STATUS_LANG_MASK
    lang = body[1 : 1 + n].decode("ascii", errors="replace")
    raw = body[1 + n :]
    if status & STATUS_UTF16:
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-16-be"
    else:
        encoding = "utf-8"
    return raw.decode(encoding, errors="replace"), lang


def parse(data):
    """Return the records of the message. Empty list if the message is empty.

    Three record shapes come back:

        {"type": "text",  "value": str}
        {"type": "uri",   "value": str}
        {"type": "other", "tnf": int, "type_name": str, "payload": bytes}

    The last one exists because silently dropping what we cannot decode is the
    worst possible behaviour: a tag carrying a vCard, a MIME payload or an
    Android Application Record then read exactly like a blank tag, with nothing
    to tell them apart. The caller now sees that there is content, and which.
    """
    payload = _extract_message(data)
    if not payload:
        return []

    records = []
    i = 0
    while i < len(payload):
        header = payload[i]
        tnf = header & TNF_MASK
        short = bool(header & FLAG_SR)
        has_id = bool(header & FLAG_IL)
        i += 1
        if i >= len(payload):
            raise NdefError("truncated record: missing type length")
        type_length = payload[i]
        i += 1
        if short:
            if i >= len(payload):
                raise NdefError("truncated record: missing length")
            payload_length = payload[i]
            i += 1
        else:
            if i + 4 > len(payload):
                raise NdefError("truncated record: missing long length")
            payload_length = int.from_bytes(payload[i : i + 4], "big")
            i += 4

        # The ID_LENGTH field only exists when the IL flag is set. Forgetting
        # it shifted everything after it by one byte: the type read wrong and
        # the payload came out truncated, with no error to say so. Records with
        # an ID are common — Android produces them.
        id_length = 0
        if has_id:
            if i >= len(payload):
                raise NdefError("truncated record: missing ID length")
            id_length = payload[i]
            i += 1

        record_type = payload[i : i + type_length]
        i += type_length
        i += id_length  # the ID itself is of no interest to this module
        body = payload[i : i + payload_length]
        i += payload_length

        if len(body) != payload_length:
            raise NdefError("truncated record payload")

        # Checking the TNF is not cosmetic: "T" and "U" only mean text and URI
        # in the Well Known name space. The same type name under another TNF is
        # a different type, and decoding it as text would invent content.
        known = tnf == TNF_WELL_KNOWN and bool(body)
        if known and record_type == RTD_TEXT:
            value, _lang = _decode_text(body)
            records.append({"type": "text", "value": value})
        elif known and record_type == RTD_URI:
            index = body[0]
            prefix = URI_PREFIXES[index] if index < len(URI_PREFIXES) else ""
            records.append(
                {
                    "type": "uri",
                    "value": prefix + body[1:].decode("utf-8", errors="replace"),
                }
            )
        else:
            records.append(
                {
                    "type": "other",
                    "tnf": tnf,
                    "type_name": record_type.decode("ascii", errors="replace"),
                    "payload": bytes(body),
                }
            )

        if header & FLAG_ME:
            break

    return records


def _printable(text):
    """Escape non-printable characters coming from a tag."""
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def format_record(record):
    """One readable line for a record, whatever its type.

    Records of type "other" have no `value` field: displaying them means
    knowing which of the two shapes is at hand, and that knowledge lives here
    rather than being copied into every tool.
    """
    if record["type"] == "other":
        payload = record["payload"]
        preview = payload[:16].hex(" ")
        if len(payload) > 16:
            preview += " ..."
        name = _printable(record["type_name"]) or "?"
        return f"{name} (tnf {record['tnf']}, {len(payload)} bytes) {preview}"
    return _printable(record["value"])
