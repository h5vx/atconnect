import socket
import struct
import threading

import pytest

from atconnect.client import CommandError, RtkClient
from atconnect.device import Headphones
from atconnect.protocol import (
    At,
    AtMessage,
    AtNotify,
    Codec,
    Decoder,
    Op,
    Packet,
    ack_packet,
    parse_cmd_set_info,
    parse_fw_version,
)


def test_packet_roundtrip():
    pkt = Packet(7, Op.AT_SEND, bytes.fromhex("0015"))
    raw = pkt.encode()
    assert raw == bytes.fromhex("aa07 0400 0008 0015")
    assert Decoder().feed(raw) == [pkt]


def test_decoder_handles_split_and_garbage():
    a = Packet(1, 0x0019, b"\x02\x50\x00").encode()
    b = ack_packet(2, Op.AT_SEND).encode()
    d = Decoder()
    assert d.feed(b"\x00\x13" + a[:3]) == []
    assert d.feed(a[3:] + b[:5]) == [Packet(1, 0x0019, b"\x02\x50\x00")]
    out = d.feed(b[5:])
    assert out[0].ack_info() == (Op.AT_SEND, 0)


def test_at_message_parse():
    resp = AtMessage.parse(bytes.fromhex("0015 00 04"))
    assert (resp.sub_id, resp.status, resp.payload) == (At.GET_CODEC_MODE, 0, b"\x04")
    note = AtMessage.parse(struct.pack(">H", AtNotify.LOW_LATENCY_MODE) + b"\x01")
    assert note.is_notification and note.payload == b"\x01"


def test_fw_version_and_cmd_set_info():
    assert parse_fw_version(bytes([4, 1, 2, 3, 4])) == "1.2.3.4"
    assert parse_fw_version(bytes(9) + bytes([0, 1, 5, 0])) == "0.1.5.0"
    assert parse_cmd_set_info(bytes.fromhex("0000 0102 0004")) == (0x0102, 4)
    assert parse_cmd_set_info(bytes.fromhex("0000 0103")) == (0x0103, 4)


class FakeHeadphones(threading.Thread):
    """Minimal device emulator: ACKs everything and answers a few commands."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.seq = 0x80
        self.codec = Codec.AUTO
        self.received = []

    def send(self, opcode, params=b""):
        self.seq += 1
        self.sock.sendall(Packet(self.seq, opcode, params).encode())

    def run(self):
        dec = Decoder()
        while data := self.sock.recv(4096):
            for pkt in dec.feed(data):
                self.received.append(pkt)
                if pkt.is_ack:
                    continue
                self.seq += 1
                self.sock.sendall(ack_packet(self.seq, pkt.opcode).encode())
                self.answer(pkt)

    def answer(self, pkt):
        if pkt.opcode == Op.GET_STATUS:
            self.send(Op.STATUS_REPORT, bytes([pkt.params[0], 77, 0]))
        elif pkt.opcode == Op.AT_SEND:
            sub, payload = struct.unpack_from(">H", pkt.params)[0], pkt.params[2:]
            if sub == At.GET_CODEC_MODE:
                self.send(Op.AT_RECEIVE, struct.pack(">HB", sub, 0) + bytes([self.codec]))
            elif sub == At.SET_CODEC_MODE:
                self.codec = Codec(payload[0])
                self.send(Op.AT_RECEIVE, struct.pack(">HB", sub, 0))
            else:
                self.send(Op.AT_RECEIVE, struct.pack(">HB", sub, 2))


@pytest.fixture
def device():
    a, b = socket.socketpair()
    fake = FakeHeadphones(b)
    fake.start()
    client = RtkClient(a)
    yield Headphones(client), fake
    client.close()
    a.close()
    b.close()


def test_client_against_fake(device):
    hp, fake = device
    assert hp.battery() == 77
    assert hp.codec() == Codec.AUTO
    hp.set_codec(Codec.LDAC)
    assert hp.codec() == Codec.LDAC
    with pytest.raises(CommandError):
        hp.low_latency()
    # The client must ACK every event it received.
    acked = {p.ack_info()[0] for p in fake.received if p.is_ack}
    assert {Op.STATUS_REPORT, Op.AT_RECEIVE} <= acked


def test_notifications_are_dispatched(device):
    hp, fake = device
    got = threading.Event()
    seen = []
    hp.client.on_at_notification.append(lambda m: (seen.append(m), got.set()))
    fake.send(Op.AT_RECEIVE, struct.pack(">H", AtNotify.STREAMING_CODEC) + b"\x04")
    assert got.wait(2)
    assert seen[0].sub_id == AtNotify.STREAMING_CODEC and seen[0].payload == b"\x04"
