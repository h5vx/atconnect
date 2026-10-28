"""Realtek "BeePro" transport framing and Audio-Technica command tables.

Wire format (RFCOMM, little endian unless noted):

    AA <seq> <len:u16> <opcode:u16> <params...>        len = 2 + len(params)

Opcode 0x0000 is an ACK whose params are <acked opcode:u16> <status:u8>.
Every packet except ACKs and opcode 0x0301 must be ACKed by the receiver.

Audio-Technica specific commands are tunnelled through vendor opcode 0x0800:

    request  0x0800  <sub id:u16 BE> <payload...>
    response 0x8800  <sub id:u16 BE> <status:u8> <payload...>
    notify   0x8800  <sub id:u16 BE> <payload...>        (sub id in AtNotify)
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass

SYNC = 0xAA
HEADER_LEN = 4

# The vendor RFCOMM service exposed by Realtek based Audio-Technica headphones.
RTK_SPP_UUID = "6a24eeab-4b65-4693-986b-3c26c352264f"
# ATH-M50xBT2 does not advertise the vendor UUID and serves the protocol on plain SPP.
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"


class Op(enum.IntEnum):
    ACK = 0x0000
    MMI = 0x0004
    GET_CMD_SET_INFO = 0x000C
    CMD_SET_INFO = 0x0011  # event
    SET_CFG = 0x0012
    GET_CFG = 0x0017
    GET_STATUS = 0x0018
    CFG_REPORT = 0x0018  # event, shares the value with the GET_STATUS request
    STATUS_REPORT = 0x0019  # event
    EQ_QUERY_INFO = 0x0200  # request and event
    EQ_ENABLE = 0x0201
    EQ_DISABLE = 0x0202
    EQ_INDEX_REPORT = 0x0202  # event, shares the value with EQ_DISABLE
    EQ_SET_INDEX = 0x0205
    EQ_GET_INDEX = 0x0206
    EQ_GET_INDEX_V0 = 0x0206  # event for older spec
    GET_FW_VERSION = 0x0308
    FW_VERSION_REPORT = 0x0909  # event
    AT_SEND = 0x0800
    AT_RECEIVE = 0x8800


class Ack(enum.IntEnum):
    COMPLETE = 0
    DISALLOW = 1
    UNKNOWN_COMMAND = 2
    PARAMETERS_ERROR = 3
    BUSY = 4
    PROCESS_FAIL = 5


# Opcodes the device sends that must not be ACKed.
NO_ACK_OPCODES = {Op.ACK, 0x0301}


class At(enum.IntEnum):
    """Audio-Technica sub commands (subset relevant to Realtek headphones)."""

    GET_CODEC_MODE = 21
    SET_CODEC_MODE = 22
    GET_STREAMING_CODEC = 23
    GET_AMBIENCE_MODE = 28
    SET_AMBIENCE_MODE = 29
    GET_VOICE_ASSISTANT = 41
    SET_VOICE_ASSISTANT = 42
    GET_VOLUME_STEP = 47
    SET_VOLUME_STEP = 48
    GET_VOLUME_LEVEL = 49
    SET_VOLUME_LEVEL = 50
    GET_VOLUME_BALANCE = 52
    SET_VOLUME_BALANCE = 53
    GET_MULTIPOINT_SETTING = 54
    SET_MULTIPOINT_SETTING = 55
    DEVICE_RESET = 57
    AV_REMOTE_CONTROL = 58
    GET_AUDIO_PROMPT_CONTROL = 59
    SET_AUDIO_PROMPT_CONTROL = 60
    SAVE_EQ_PARAMETER = 61
    GET_EQ_NICKNAME = 62
    SET_EQ_NICKNAME = 63
    GET_LOW_LATENCY_MODE = 64
    SET_LOW_LATENCY_MODE = 65
    GET_GFPS_MODEL_ID = 76
    GET_NUMBER_OF_APP_CONNECTIONS = 77
    GET_PAIRED_DEVICE_LIST = 80
    GET_PAIRED_DEVICE_INFO = 81
    CONNECT_TO_PAIRED_DEVICE = 82
    DISCONNECT_PAIRED_DEVICE = 83
    REMOVE_PAIRED_DEVICE = 87
    ENTER_PAIRING_MODE = 89
    CANCEL_PAIRING_MODE = 90
    GET_APP_BD_ADDRESS = 91
    GET_SIDETONE_SETTING = 96
    SET_SIDETONE_SETTING = 97
    GET_PRODUCT_ID = 108
    GET_BREDR_CONNECTION_STATE = 109
    FACTORY_RESET = 123
    GET_VP_ID = 137
    GET_AUTO_POWER_OFF_SETTING = 172
    SET_AUTO_POWER_OFF_SETTING = 173
    GET_STREAMING_SAMPLING_FREQUENCY = 235


class AtNotify(enum.IntEnum):
    AMBIENCE_MODE = 30
    TALK_THROUGH_STATE = 31
    VOLUME_LEVEL = 51
    LOW_LATENCY_MODE = 66
    SCO_LINK_STATE = 79
    DEVICE_CONNECTION_STATE = 85
    ACTIVE_DEVICE = 86
    REMOVED_PAIRED_DEVICE = 88
    CHANGE_IN_PLAYING_MEDIA = 95
    STREAMING_CODEC = 98
    TOUCH_EVENT = 129
    TOUCH_SENSOR_REG_VALS = 156
    EQ_STATE = 165
    BUDS_TIMER_STATE = 194
    AUDIO_PATH_SELECT = 211
    MUTE_AND_EXTERNAL_FOCUS = 213
    LEA_DEVICE_CONNECTION_STATE = 217
    BROADCAST_STATE = 219
    EAR_DETECTION_STATE = 225
    HS_CURRENT_VOLUME = 241
    NOISE_CANCEL_TYPE = 244
    LIMIT_SOUND_LEAKAGE = 247


AT_NOTIFY_IDS = {int(n) for n in AtNotify}


class Codec(enum.IntEnum):
    AUTO = 0
    APTX = 1
    AAC = 2
    SBC = 3
    LDAC = 4
    APTX_HD = 5
    APTX_ADAPTIVE = 6
    LHDC = 7
    LDAC_AAC = 8
    LHDC_AAC = 9


class StreamingCodec(enum.IntEnum):
    """Codec currently in use (GET_STREAMING_CODEC / STREAMING_CODEC notification)."""

    NONE = 0
    SBC = 1
    AAC = 2
    APTX = 3
    APTX_LL = 4
    APTX_HD = 5
    LDAC = 6
    APTX_ADAPTIVE = 7
    LHDC = 8
    LC3 = 9


class VoiceAssistant(enum.IntEnum):
    DEFAULT = 0
    AMA = 1  # Amazon Alexa
    GAA = 2  # Google Assistant


class VoicePrompt(enum.IntEnum):
    VOICE = 0
    BEEP = 1


class StatusIndex(enum.IntEnum):
    RWS_STATE = 0
    RWS_CHANNEL = 1
    BATTERY = 2
    APT = 3
    VOLUME = 0xA1


# MMI action codes (opcode 0x0004).
class Mmi(enum.IntEnum):
    ENTER_PAIRING = 0x51
    EXIT_PAIRING = 0x52
    FACTORY_RESET = 0x5A
    CLEAR_PAIRED_LIST = 0x5B


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Packet:
    seq: int
    opcode: int
    params: bytes

    def encode(self) -> bytes:
        body = struct.pack("<H", self.opcode) + self.params
        return struct.pack("<BBH", SYNC, self.seq & 0xFF, len(body)) + body

    @property
    def is_ack(self) -> bool:
        return self.opcode == Op.ACK

    def ack_info(self) -> tuple[int, int]:
        """(acked opcode, status) of an ACK packet."""
        if len(self.params) < 3:
            raise ProtocolError(f"short ACK: {self.params.hex()}")
        opcode, status = struct.unpack_from("<HB", self.params)
        return opcode, status

    def __str__(self) -> str:
        name = _op_name(self.opcode)
        if self.is_ack and len(self.params) >= 3:
            op, st = self.ack_info()
            return f"[{self.seq:02x}] ACK {_op_name(op)} status={st}"
        return f"[{self.seq:02x}] {name} {self.params.hex(' ')}"


_OP_NAMES: dict[int, str] = {}
for _name, _member in Op.__members__.items():
    _OP_NAMES[_member.value] = f"{_OP_NAMES[_member.value]}|{_name}" if _member.value in _OP_NAMES else _name


def _op_name(op: int) -> str:
    return _OP_NAMES.get(op, f"0x{op:04x}")


def ack_packet(seq: int, opcode: int, status: int = 0) -> Packet:
    return Packet(seq, Op.ACK, struct.pack("<HB", opcode, status))


class Decoder:
    """Incremental stream decoder: feed() raw bytes, get complete packets back."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self._buf += data
        out: list[Packet] = []
        while True:
            start = self._buf.find(SYNC)
            if start < 0:
                self._buf.clear()
                return out
            if start:
                del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                return out
            _, seq, length = struct.unpack_from("<BBH", self._buf)
            if length < 2:
                # Not a real header, resync on the next sync byte.
                del self._buf[:1]
                continue
            if len(self._buf) < HEADER_LEN + length:
                return out
            (opcode,) = struct.unpack_from("<H", self._buf, HEADER_LEN)
            params = bytes(self._buf[HEADER_LEN + 2 : HEADER_LEN + length])
            del self._buf[: HEADER_LEN + length]
            out.append(Packet(seq, opcode, params))


@dataclass(frozen=True)
class AtMessage:
    """A decoded 0x8800 vendor packet."""

    sub_id: int
    status: int | None  # None for notifications
    payload: bytes

    @property
    def is_notification(self) -> bool:
        return self.status is None

    @classmethod
    def parse(cls, params: bytes) -> AtMessage:
        if len(params) < 2:
            raise ProtocolError(f"short AT packet: {params.hex()}")
        (sub_id,) = struct.unpack_from(">H", params)
        if sub_id in AT_NOTIFY_IDS:
            return cls(sub_id, None, params[2:])
        if len(params) < 3:
            return cls(sub_id, Ack.PROCESS_FAIL, b"")
        return cls(sub_id, params[2], params[3:])


def at_request(sub_id: int, payload: bytes = b"") -> bytes:
    return struct.pack(">H", sub_id) + payload


def parse_fw_version(params: bytes) -> str:
    if params and params[0] == 4 and len(params) >= 5:
        v = params[1:5]
    elif len(params) >= 13:
        v = params[9:13]
    else:
        raise ProtocolError(f"unexpected FW version report: {params.hex()}")
    return ".".join(str(b) for b in v)


def parse_cfg_report(params: bytes) -> tuple[int, bytes]:
    """CFG_REPORT: <type> <len> <data>. Returns (type, data)."""
    if len(params) < 2 or len(params) < params[1] + 2:
        raise ProtocolError(f"bad cfg report: {params.hex()}")
    return params[0], params[2 : 2 + params[1]]


def parse_cmd_set_info(params: bytes) -> tuple[int, int] | None:
    """CMD_SET_INFO for query type 0: returns (cmd set version, eq spec version)."""
    if len(params) < 2 or params[0] != 0 or params[1] != 0:
        return None
    cmd_set = struct.unpack_from(">H", params, 2)[0] if len(params) >= 4 else 0
    if len(params) >= 6:
        eq_spec = struct.unpack_from(">H", params, 4)[0]
    else:
        eq_spec = {0: 0, 1: 0, 256: 1, 257: 2, 258: 3, 259: 4, 260: 5}.get(cmd_set, 0)
    return cmd_set, eq_spec
