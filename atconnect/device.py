"""High level API for Realtek based Audio-Technica headphones (ATH-M50xBT2)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .client import CommandError, RtkClient
from .protocol import (
    At,
    Codec,
    Mmi,
    Op,
    Packet,
    ProtocolError,
    StatusIndex,
    StreamingCodec,
    VoiceAssistant,
    VoicePrompt,
    parse_cfg_report,
    parse_cmd_set_info,
    parse_fw_version,
)


@dataclass(frozen=True)
class EqPreset:
    bank: int
    name: str
    custom: bool = False


@dataclass(frozen=True)
class Model:
    name: str
    codecs: tuple[Codec, ...]
    low_latency_unsupported: tuple[Codec, ...]
    eq_presets: tuple[EqPreset, ...]
    eq_bands_hz: tuple[float, ...]
    sidetone_gains: tuple[str, ...]
    name_max_len: int


# Values taken from the model definition in the official app (EQ "V2", 5 band GEQ).
M50XBT2 = Model(
    name="ATH-M50xBT2",
    codecs=(Codec.AUTO, Codec.LDAC, Codec.AAC, Codec.SBC),
    low_latency_unsupported=(Codec.AUTO, Codec.LDAC, Codec.LHDC),
    eq_presets=(
        EqPreset(3, "Original"),
        EqPreset(5, "Bass Boost"),
        EqPreset(6, "Clear Vocal"),
        EqPreset(4, "V-shaped"),
        EqPreset(7, "Treble Enhance"),
        EqPreset(1, "MySetting1", custom=True),
    ),
    eq_bands_hz=(82, 247, 1000, 4000, 12500),
    sidetone_gains=("Low", "High"),
    name_max_len=16,
)


def _byte(payload: bytes, what: str) -> int:
    if not payload:
        raise ProtocolError(f"empty response for {what}")
    return payload[0]


def _signed(b: int) -> int:
    return b - 256 if b > 127 else b


class Headphones:
    def __init__(self, client: RtkClient, model: Model = M50XBT2) -> None:
        self.client = client
        self.model = model
        self._eq_spec: int | None = None

    # ---- Realtek generic commands -------------------------------------

    def battery(self) -> int:
        pkt = self.client.request(
            Op.GET_STATUS,
            bytes([StatusIndex.BATTERY]),
            lambda p: p.opcode == Op.STATUS_REPORT and p.params[:1] == bytes([StatusIndex.BATTERY]),
        )
        assert pkt
        if len(pkt.params) < 2:
            raise ProtocolError(f"bad battery report: {pkt.params.hex()}")
        return pkt.params[1]

    def name(self) -> str:
        pkt = self.client.request(
            Op.GET_CFG, b"\x01", lambda p: p.opcode == Op.CFG_REPORT and p.params[:1] == b"\x01"
        )
        assert pkt
        _, data = parse_cfg_report(pkt.params)
        return data.decode("utf-8", "replace").strip("\x00 ")

    def set_name(self, name: str) -> None:
        data = name.encode("utf-8")
        if not data or len(data) > self.model.name_max_len:
            raise ValueError(f"name must be 1..{self.model.name_max_len} bytes of UTF-8")
        self.client.request(Op.SET_CFG, bytes([1, len(data)]) + data)

    def firmware_version(self) -> str:
        def is_report(p: Packet) -> bool:
            return p.opcode == Op.FW_VERSION_REPORT

        for query in (b"\x04", b"\x00"):
            try:
                pkt = self.client.request(Op.GET_FW_VERSION, query, is_report)
                assert pkt
                return parse_fw_version(pkt.params)
            except (CommandError, TimeoutError, ProtocolError):
                continue
        raise ProtocolError("firmware version not available")

    def enter_pairing_mode(self) -> None:
        self.client.request(Op.MMI, bytes([0, Mmi.ENTER_PAIRING]))

    # ---- Equalizer (Realtek EQ model) ---------------------------------

    def eq_spec_version(self) -> int:
        if self._eq_spec is None:
            pkt = self.client.request(
                Op.GET_CMD_SET_INFO, b"\x00", lambda p: p.opcode == Op.CMD_SET_INFO and p.params[:1] == b"\x00"
            )
            info = parse_cmd_set_info(pkt.params) if pkt else None
            self._eq_spec = info[1] if info else 0
        return self._eq_spec

    def eq_enabled(self) -> bool:
        pkt = self.client.request(
            Op.EQ_QUERY_INFO, b"\x00", lambda p: p.opcode == Op.EQ_QUERY_INFO and p.params[:1] == b"\x00"
        )
        assert pkt
        return len(pkt.params) >= 2 and pkt.params[1] == 1

    def set_eq_enabled(self, on: bool) -> None:
        self.client.request(Op.EQ_ENABLE if on else Op.EQ_DISABLE)

    def eq_bank(self) -> int:
        spec = self.eq_spec_version()
        params = b"\x00" if spec >= 4 else b""
        pkt = self.client.request(
            Op.EQ_GET_INDEX, params, lambda p: p.opcode in (Op.EQ_INDEX_REPORT, Op.EQ_GET_INDEX_V0) and bool(p.params)
        )
        assert pkt
        return pkt.params[0]

    def set_eq_bank(self, bank: int) -> None:
        spec = self.eq_spec_version()
        if spec >= 4:
            self.client.request(Op.EQ_SET_INDEX, bytes([bank & 0xFF, 0]))
        elif spec >= 1:
            self.client.request(Op.EQ_SET_INDEX, bytes([bank & 0xFF]))
        else:
            self.client.request(0x0208, struct.pack("<H", bank))

    def eq_preset(self) -> EqPreset | None:
        bank = self.eq_bank()
        return next((p for p in self.model.eq_presets if p.bank == bank), None)

    # ---- Audio-Technica vendor commands -------------------------------

    def codec(self) -> Codec:
        return Codec(_byte(self.client.at(At.GET_CODEC_MODE), "codec"))

    def set_codec(self, codec: Codec) -> None:
        if codec not in self.model.codecs:
            raise ValueError(f"{codec.name} is not supported by {self.model.name}")
        self.client.at(At.SET_CODEC_MODE, bytes([codec]))

    def streaming_codec(self) -> StreamingCodec | int:
        v = _byte(self.client.at(At.GET_STREAMING_CODEC), "streaming codec")
        try:
            return StreamingCodec(v)
        except ValueError:
            return v

    def low_latency(self) -> bool:
        return _byte(self.client.at(At.GET_LOW_LATENCY_MODE), "low latency") != 0

    def set_low_latency(self, on: bool) -> None:
        self.client.at(At.SET_LOW_LATENCY_MODE, bytes([int(on)]))

    def sidetone(self) -> tuple[bool, int]:
        p = self.client.at(At.GET_SIDETONE_SETTING)
        if len(p) < 2:
            raise ProtocolError(f"bad sidetone payload: {p.hex()}")
        return p[0] != 0, p[1]

    def set_sidetone(self, on: bool, gain: int) -> None:
        self.client.at(At.SET_SIDETONE_SETTING, bytes([int(on), gain]))

    def multipoint(self) -> bool:
        return _byte(self.client.at(At.GET_MULTIPOINT_SETTING), "multipoint") != 0

    def set_multipoint(self, on: bool) -> None:
        self.client.at(At.SET_MULTIPOINT_SETTING, bytes([int(on)]))

    def voice_assistant(self) -> VoiceAssistant | int:
        v = _byte(self.client.at(At.GET_VOICE_ASSISTANT), "voice assistant")
        try:
            return VoiceAssistant(v)
        except ValueError:
            return v

    def set_voice_assistant(self, va: VoiceAssistant) -> None:
        self.client.at(At.SET_VOICE_ASSISTANT, bytes([va]))

    def voice_prompt(self) -> VoicePrompt | int:
        v = _byte(self.client.at(At.GET_AUDIO_PROMPT_CONTROL), "voice prompt")
        try:
            return VoicePrompt(v)
        except ValueError:
            return v

    def set_voice_prompt(self, vp: VoicePrompt) -> None:
        self.client.at(At.SET_AUDIO_PROMPT_CONTROL, bytes([vp]))

    def volume_step(self) -> int:
        return _byte(self.client.at(At.GET_VOLUME_STEP), "volume step")

    def set_volume_step(self, value: int) -> None:
        self.client.at(At.SET_VOLUME_STEP, bytes([value]))

    def volume_level(self) -> int:
        return _byte(self.client.at(At.GET_VOLUME_LEVEL), "volume level")

    def set_volume_level(self, value: int) -> None:
        self.client.at(At.SET_VOLUME_LEVEL, bytes([value]))

    def volume_balance(self) -> int:
        """Left/right balance; negative values shift towards the left channel."""
        return _signed(_byte(self.client.at(At.GET_VOLUME_BALANCE), "volume balance"))

    def set_volume_balance(self, value: int) -> None:
        self.client.at(At.SET_VOLUME_BALANCE, bytes([value & 0xFF]))

    def product_id(self) -> bytes:
        return self.client.at(At.GET_PRODUCT_ID)
