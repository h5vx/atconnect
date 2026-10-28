"""High level API for Realtek based Audio-Technica headphones (ATH-M50xBT2)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .client import CommandError, RtkClient
from .eq import GEQ_FREQS, GEQ_QS, PRESETS, SAMPLE_RATES, EqCurve, decode_eq
from .protocol import (
    At,
    BtProfile,
    Codec,
    Mmi,
    Op,
    PairedDevice,
    ProtocolError,
    StatusIndex,
    StreamingCodec,
    VoiceAssistant,
    VoicePrompt,
    bdaddr_bytes,
    bdaddr_str,
    format_app_version,
    parse_cfg_report,
    parse_cmd_set_info,
    parse_fw_version,
    parse_ota_app_version,
    parse_paired_device_info,
    parse_paired_device_list,
)


@dataclass(frozen=True)
class Model:
    name: str
    codecs: tuple[Codec, ...]
    low_latency_unsupported: tuple[Codec, ...]
    eq_bank: int  # bank the app writes its EQ curve to ("EQ V2")
    eq_freqs: tuple[float, ...]
    eq_qs: tuple[float, ...]
    eq_presets: dict[str, EqCurve]
    sidetone_gains: tuple[str, ...]
    name_max_len: int


# Values taken from the model definition in the official app.
M50XBT2 = Model(
    name="ATH-M50xBT2",
    codecs=(Codec.AUTO, Codec.LDAC, Codec.AAC, Codec.SBC),
    low_latency_unsupported=(Codec.AUTO, Codec.LDAC, Codec.LHDC),
    eq_bank=5,
    eq_freqs=GEQ_FREQS,
    eq_qs=GEQ_QS,
    eq_presets=PRESETS,
    sidetone_gains=("Low", "High"),
    name_max_len=16,
)

# How long to wait for an AT command the firmware may silently ignore.
PROBE_TIMEOUT = 1.5


def _byte(payload: bytes, what: str) -> int:
    if not payload:
        raise ProtocolError(f"empty response for {what}")
    return payload[0]


def _signed(b: int) -> int:
    return b - 256 if b > 127 else b


class Unsupported(Exception):
    """The connected firmware does not implement this feature."""


class Headphones:
    def __init__(self, client: RtkClient, model: Model = M50XBT2) -> None:
        self.client = client
        self.model = model
        self._eq_spec: int | None = None
        self._mp_management: bool | None = None

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

    def firmware_version_number(self) -> int:
        """Version as the app compares it (e.g. 0x1008), read through the OTA service."""
        target = self.client.request(Op.OTA_GET_TARGET_INFO, b"", lambda p: p.opcode == Op.OTA_GET_TARGET_INFO)
        images = self.client.request(Op.OTA_GET_IMAGE_INFO, b"", lambda p: p.opcode == Op.OTA_GET_IMAGE_INFO)
        assert target and images
        version = parse_ota_app_version(target.params, images.params)
        if version is None:
            raise ProtocolError("firmware version not reported")
        return version

    def firmware_version(self) -> str:
        try:
            return format_app_version(self.firmware_version_number())
        except (CommandError, TimeoutError, ProtocolError):
            pass
        for query in (b"\x04", b"\x00"):  # older Realtek firmwares
            try:
                pkt = self.client.request(
                    Op.GET_FW_VERSION, query, lambda p: p.opcode == Op.FW_VERSION_REPORT
                )
                assert pkt
                return parse_fw_version(pkt.params)
            except (CommandError, TimeoutError, ProtocolError):
                continue
        raise ProtocolError("firmware version not available")

    # ---- Equalizer (Realtek EQ model, app "EQ V2") ----------------------

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

    def eq_bank_data(self, bank: int) -> tuple[int, bytes]:
        """(sample rate code, eqData) stored in a bank (EQ spec 2/3 layout)."""
        pkt = self.client.request(
            Op.EQ_GET_PARAM, bytes([bank]), lambda p: p.opcode == Op.EQ_PARAM_REPORT and p.params[:1] == bytes([bank])
        )
        assert pkt
        p = pkt.params
        if len(p) < 6:
            raise ProtocolError(f"short EQ parameter report: {p.hex()}")
        (length,) = struct.unpack_from("<H", p, 3)
        data = p[6 : 5 + length]
        if len(data) >= 4:  # the bank is a fixed size buffer; cut at the declared stage count
            data = data[: 4 + 20 * (data[0] & 0xF)]
        return p[5], data

    def eq_curve(self) -> tuple[float, list[tuple[float, float, float]]]:
        """Decoded curve of the app EQ bank: (global gain dB, [(gain, freq, dsp Q), ...])."""
        _, data = self.eq_bank_data(self.model.eq_bank)
        # The reported sample-rate code is not reliable (the M50xBT2 answers 1); the
        # 44.1 kHz variant is the one read back.
        return decode_eq(data, 44100.0)

    def eq_preset(self) -> str | None:
        """Name of the preset currently stored in the app EQ bank, if it matches one exactly."""
        if self.eq_bank() != self.model.eq_bank:
            return None
        _, data = self.eq_bank_data(self.model.eq_bank)
        for name, curve in self.model.eq_presets.items():
            if any(curve.eq_data(fs) == data for fs in SAMPLE_RATES.values()):
                return name
        return "custom"

    def set_eq_curve(self, curve: EqCurve) -> None:
        """Write a curve to the app EQ bank for 44.1 and 48 kHz, select and save it."""
        spec = self.eq_spec_version()
        if spec not in (2, 3):
            raise Unsupported(f"EQ spec {spec} is not supported yet")
        bank = self.model.eq_bank
        for sr, fs in SAMPLE_RATES.items():
            body = bytes([sr]) + curve.eq_data(fs)
            self.client.request(Op.EQ_SET_PARAM, bytes([bank, 0, 0]) + struct.pack("<H", len(body)) + body)
        self.set_eq_bank(bank)
        self.client.at(At.SAVE_EQ_PARAMETER, bytes([bank]))

    def set_eq_preset(self, name: str) -> None:
        try:
            curve = self.model.eq_presets[name]
        except KeyError:
            raise ValueError(f"unknown preset {name!r}; choose from {', '.join(self.model.eq_presets)}") from None
        self.set_eq_curve(curve)

    def set_eq_gains(self, gains: list[float], global_gain: float = -6.0) -> None:
        if len(gains) != len(self.model.eq_freqs):
            raise ValueError(f"expected {len(self.model.eq_freqs)} band gains")
        if any(not -12 <= g <= 12 for g in gains):
            raise ValueError("band gains must be within -12..12 dB")
        self.set_eq_curve(EqCurve(tuple(gains), self.model.eq_freqs, self.model.eq_qs, global_gain))

    # ---- Multipoint ------------------------------------------------------
    #
    # The ATH-M50xBT2 firmware (1.0.0.8) only implements the on/off switch;
    # the paired-device management commands are ACKed but never answered.
    # They are implemented here for firmwares that do answer them.

    def multipoint(self) -> bool:
        return _byte(self.client.at(At.GET_MULTIPOINT_SETTING), "multipoint") != 0

    def set_multipoint(self, on: bool) -> None:
        self.client.at(At.SET_MULTIPOINT_SETTING, bytes([int(on)]))

    def multipoint_management_supported(self) -> bool:
        if self._mp_management is None:
            try:
                self.client.at(At.GET_PAIRED_DEVICE_LIST, timeout=PROBE_TIMEOUT)
                self._mp_management = True
            except (TimeoutError, CommandError):
                self._mp_management = False
        return self._mp_management

    def _mp(self, sub: At, payload: bytes = b"") -> bytes:
        if not self.multipoint_management_supported():
            raise Unsupported("this firmware does not support managing paired devices")
        return self.client.at(sub, payload)

    def paired_devices(self) -> list[PairedDevice]:
        addresses = parse_paired_device_list(self._mp(At.GET_PAIRED_DEVICE_LIST))
        return [self.paired_device(a) for a in addresses]

    def paired_device(self, address: str) -> PairedDevice:
        return parse_paired_device_info(self._mp(At.GET_PAIRED_DEVICE_INFO, bdaddr_bytes(address)))

    def connect_device(self, address: str) -> None:
        self._mp(At.CONNECT_TO_PAIRED_DEVICE, bdaddr_bytes(address))

    def disconnect_device(self, address: str) -> None:
        self._mp(At.DISCONNECT_PAIRED_DEVICE, bdaddr_bytes(address))

    def set_device_profile(self, address: str, profile: BtProfile, connect: bool) -> None:
        if profile not in (BtProfile.A2DP, BtProfile.HFP):
            raise ValueError("profile must be A2DP or HFP")
        self._mp(At.CHANGE_PROFILE_OF_CONNECTED_DEVICE, bdaddr_bytes(address) + bytes([profile, int(connect)]))

    def remove_device(self, address: str) -> None:
        self._mp(At.REMOVE_PAIRED_DEVICE, bdaddr_bytes(address))

    def own_address(self) -> str:
        """Address the headphones see for this computer."""
        payload = self._mp(At.GET_APP_BD_ADDRESS)
        return bdaddr_str(payload[:6])

    def start_pairing(self) -> bool:
        """Make the headphones discoverable for another device.

        Returns True if the current link survives (firmware-level multipoint
        pairing), False if the generic Realtek pairing mode was used, which
        drops the link to this computer on the ATH-M50xBT2.
        """
        if self.multipoint_management_supported():
            self.client.at(At.ENTER_PAIRING_MODE)
            return True
        self.client.request(Op.MMI, bytes([0, Mmi.ENTER_PAIRING]))
        return False

    def cancel_pairing(self) -> None:
        if self.multipoint_management_supported():
            self.client.at(At.CANCEL_PAIRING_MODE)
        else:
            self.client.request(Op.MMI, bytes([0, Mmi.EXIT_PAIRING]))

    def reconnect_last_device(self) -> None:
        """Ask the headphones to link back to the most recently used device(s)."""
        self.client.request(Op.MMI, bytes([0, Mmi.LINK_LAST_DEVICE]))

    enter_pairing_mode = start_pairing

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
