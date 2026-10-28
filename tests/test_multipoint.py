import struct

import pytest

from atconnect.device import Unsupported
from atconnect.multipoint import LinkTracker
from atconnect.protocol import (
    At,
    BtProfile,
    LinkProfile,
    Op,
    Packet,
    format_app_version,
    parse_link_event,
    parse_ota_app_version,
    parse_paired_device_info,
    parse_paired_device_list,
)

from .test_protocol import FakeHeadphones, device  # noqa: F401  (fixture)

PHONE = "AA:BB:CC:DD:EE:01"
LAPTOP = "12:34:56:78:9A:BC"


def test_paired_device_parsing():
    payload = bytes([2]) + bytes.fromhex("AABBCCDDEE01") + bytes.fromhex("123456789ABC")
    assert parse_paired_device_list(payload) == [PHONE, LAPTOP]
    info = bytes.fromhex("AABBCCDDEE01") + bytes([5]) + b"Pixel" + bytes([3, 1, 3])
    d = parse_paired_device_info(info)
    assert (d.address, d.name, d.active) == (PHONE, "Pixel", True)
    assert d.connected == BtProfile.A2DP | BtProfile.HFP and d.supported == BtProfile.A2DP | BtProfile.HFP


def test_link_events_are_little_endian():
    # Captured from an ATH-M50xBT2 while toggling A2DP from this computer.
    off = Packet(0x68, Op.PROFILE_DISCONNECTED, bytes.fromhex("01 bc9a78563412 0000"))
    on = Packet(0x6A, Op.PROFILE_CONNECTED, bytes.fromhex("01 bc9a78563412"))
    assert parse_link_event(off).address == LAPTOP and not parse_link_event(off).connected
    assert parse_link_event(on).profile == LinkProfile.A2DP and parse_link_event(on).connected


def test_firmware_version_from_ota_info():
    # Real answers of an ATH-M50xBT2 (firmware 1.0.0.8).
    target = bytes.fromhex("0405001500000000a0aa2a00")
    images = bytes.fromhex(
        "0800000100000000 00a05100 43b01e00 966b0000 966b0000 cd600000 08000001 ffffffff 00000000".replace(" ", "")
    )
    v = parse_ota_app_version(target, images)
    assert v == 0x1008 and format_app_version(v) == "1.0.0.8"


class _Client:
    def __init__(self):
        self.on_packet = []


def test_link_tracker():
    client = _Client()
    tracker = LinkTracker(client, LAPTOP)
    seen = []
    tracker.on_change.append(lambda ev, link: seen.append((ev.connected, link.address)))
    phone_le = bytes.fromhex("AABBCCDDEE01")[::-1]
    for cb in client.on_packet:
        cb(Packet(1, Op.PROFILE_CONNECTED, bytes([LinkProfile.A2DP]) + phone_le))
        cb(Packet(2, Op.PROFILE_CONNECTED, bytes([LinkProfile.HFP]) + phone_le))
        cb(Packet(3, Op.PROFILE_DISCONNECTED, bytes([LinkProfile.A2DP]) + phone_le + b"\0\0"))
    (phone,) = tracker.other_devices
    assert phone.address == PHONE and phone.profiles == LinkProfile.HFP
    assert tracker.links[0].is_this_computer
    assert seen == [(True, PHONE), (True, PHONE), (False, PHONE)]


class MultipointFake(FakeHeadphones):
    """Fake firmware that implements paired-device management."""

    def __init__(self, sock, supported=True):
        super().__init__(sock)
        self.supported = supported
        self.calls = []

    def answer(self, pkt):
        if pkt.opcode != Op.AT_SEND:
            return super().answer(pkt)
        sub, payload = struct.unpack_from(">H", pkt.params)[0], pkt.params[2:]
        self.calls.append((sub, payload))
        if not self.supported and sub in (At.GET_PAIRED_DEVICE_LIST, At.GET_PAIRED_DEVICE_INFO):
            return  # real M50xBT2 behaviour: ACK, then silence
        if sub == At.GET_PAIRED_DEVICE_LIST:
            body = bytes([1]) + bytes.fromhex("AABBCCDDEE01")
        elif sub == At.GET_PAIRED_DEVICE_INFO:
            body = payload + bytes([3]) + b"Tab" + bytes([1, 0, 3])
        else:
            body = b""
        self.send(Op.AT_RECEIVE, struct.pack(">HB", sub, 0) + body)


@pytest.fixture
def mp_device(request):
    import socket

    from atconnect.client import RtkClient
    from atconnect.device import Headphones

    a, b = socket.socketpair()
    fake = MultipointFake(b, supported=request.param)
    fake.start()
    client = RtkClient(a)
    yield Headphones(client), fake
    client.close()
    a.close()
    b.close()


@pytest.mark.parametrize("mp_device", [True], indirect=True)
def test_management_commands(mp_device):
    hp, fake = mp_device
    (d,) = hp.paired_devices()
    assert (d.address, d.name, d.connected, d.active) == (PHONE, "Tab", BtProfile.A2DP, False)
    hp.set_device_profile(PHONE, BtProfile.HFP, True)
    hp.remove_device(PHONE)
    assert (At.CHANGE_PROFILE_OF_CONNECTED_DEVICE, bytes.fromhex("AABBCCDDEE01 02 01")) in fake.calls
    assert (At.REMOVE_PAIRED_DEVICE, bytes.fromhex("AABBCCDDEE01")) in fake.calls


@pytest.mark.parametrize("mp_device", [False], indirect=True)
def test_management_unsupported(mp_device):
    hp, _ = mp_device
    assert not hp.multipoint_management_supported()
    with pytest.raises(Unsupported):
        hp.paired_devices()
