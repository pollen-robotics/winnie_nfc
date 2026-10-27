import pytest

from winnie_nfc.transport import DesyncError, Transport


class FakeSerial:
    """Simulated serial link: one byte per operation, like the real CLRC663."""

    def __init__(self, responses):
        self.responses = bytearray(responses)
        self.written = bytearray()
        self.timeout = 0.3

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        taken = self.responses[:n]
        del self.responses[:n]
        return bytes(taken)

    def reset_input_buffer(self):
        pass


def transport_with(responses):
    t = Transport()
    t._ser = FakeSerial(responses)
    return t


def test_a_simple_read_returns_the_value():
    t = transport_with(b"\x1a")
    assert t.read_reg(0x7F) == 0x1A
    assert t._ser.written == b"\xff"  # 0x7F | 0x80


def test_a_write_sends_two_bytes_and_consumes_the_echo():
    t = transport_with(b"\x1f")
    t.write_reg(0x1F, 0x5A)
    assert t._ser.written == b"\x1f\x5a"


def test_a_mixed_pipeline_keeps_the_order():
    # write, read, write, read -> echo, value, echo, value
    t = transport_with(b"\x1f\x11\x1f\x22")
    res = t.execute([("w", 0x1F, 0x11), ("r", 0x1F),
                     ("w", 0x1F, 0x22), ("r", 0x1F)])
    assert res == [None, 0x11, None, 0x22]


def test_a_wrong_echo_raises_desync():
    t = transport_with(b"\x99")  # 0x1f expected
    with pytest.raises(DesyncError):
        t.write_reg(0x1F, 0x5A)


def test_a_truncated_response_raises_desync():
    t = transport_with(b"\x1a")  # a single response for two reads
    with pytest.raises(DesyncError):
        t.read_regs([0x7F, 0x7F])


class FragmentedFakeSerial:
    """Serial link returning data in fragments (1 byte per read)."""

    def __init__(self, responses):
        self.responses = bytearray(responses)
        self.written = bytearray()
        self.timeout = 0.3

    def write(self, data):
        self.written += data
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        # Return at most 1 byte at a time, to force fragmentation.
        taken = self.responses[:1]
        del self.responses[:1]
        return bytes(taken)

    def reset_input_buffer(self):
        pass


def fragmented_transport_with(responses):
    t = Transport()
    t._ser = FragmentedFakeSerial(responses)
    return t


def test_raw_write_sends_without_waiting_for_a_response():
    """raw_write() must send with no echo check and no wait for a response."""
    t = Transport()

    class ReadTrackingFakeSerial:
        def __init__(self):
            self.written = bytearray()
            self.read_called = False
            self.timeout = 0.3

        def write(self, data):
            self.written += data
            return len(data)

        def flush(self):
            pass

        def read(self, n):
            self.read_called = True
            return b""

        def reset_input_buffer(self):
            pass

    ser = ReadTrackingFakeSerial()
    t._ser = ser

    # Send 4 bytes
    t.raw_write(b"\xFF\x81\x42\x00")

    # Check the bytes went out untouched
    assert ser.written == b"\xFF\x81\x42\x00"
    # raw_write must never call read()
    assert not ser.read_called


def test_drain_empties_the_input_buffer():
    """drain() must call reset_input_buffer()."""
    t = Transport()

    class DrainTrackingFakeSerial:
        def __init__(self):
            self.reset_called = False
            self.timeout = 0.3

        def reset_input_buffer(self):
            self.reset_called = True

    ser = DrainTrackingFakeSerial()
    t._ser = ser

    t.drain()
    assert ser.reset_called


def test_a_fragmented_read_accumulates_the_bytes():
    """The accumulation loop must collect bytes arriving in fragments."""
    # Provide 4 responses that will arrive one byte at a time
    t = fragmented_transport_with(b"\x1f\x11\x1f\x22")

    res = t.execute([("w", 0x1F, 0x11), ("r", 0x1F),
                     ("w", 0x1F, 0x22), ("r", 0x1F)])

    # Check every byte was accumulated and interpreted correctly
    assert res == [None, 0x11, None, 0x22]


def test_a_batch_of_more_than_512_operations_is_split():
    """A batch of more than 512 operations must be split automatically."""
    # 520 read operations (> 512)
    ops = [("r", 0x10 + (i % 16)) for i in range(520)]

    # Provide exactly 520 response bytes (i % 256 to avoid collisions)
    responses = bytes(i % 256 for i in range(520))

    t = Transport()
    t._ser = FakeSerial(responses)

    res = t.execute(ops)

    # Check every response is present, and in order
    assert len(res) == 520
    for i, val in enumerate(res):
        assert val == (i % 256)
