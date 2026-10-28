"""Command line interface: `atconnect --help`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager

from . import bluez
from .client import CommandError, RtkClient
from .device import Headphones
from .protocol import AtMessage, AtNotify, Codec, Op, Packet, ProtocolError, VoiceAssistant, VoicePrompt

ON_OFF = {"on": True, "off": False, "1": True, "0": False, "true": True, "false": False}
ASSISTANTS = {"default": VoiceAssistant.DEFAULT, "alexa": VoiceAssistant.AMA, "google": VoiceAssistant.GAA}


def _on_off(v: str) -> bool:
    try:
        return ON_OFF[v.lower()]
    except KeyError:
        raise argparse.ArgumentTypeError("expected on/off") from None


@contextmanager
def open_headphones(args):
    dev = bluez.find_device(args.device)
    conn = bluez.connect(dev, channel=args.channel)
    client = RtkClient(conn.sock)
    try:
        yield Headphones(client)
    finally:
        client.close()
        conn.close()


def _try(fn: Callable[[], object]) -> str:
    try:
        return str(fn())
    except (CommandError, TimeoutError, ProtocolError, ValueError) as e:
        return f"n/a ({e})"


def cmd_devices(args) -> None:
    devices = bluez.list_devices(only_supported=not args.all)
    if not devices:
        print("no paired Audio-Technica headphones")
    for d in devices:
        mark = "*" if d.has_vendor_service else " "
        print(f"{mark} {d}")


def cmd_status(args) -> None:
    with open_headphones(args) as hp:
        def preset():
            p = hp.eq_preset()
            return p.name if p else f"bank {hp.eq_bank()}"

        def sidetone():
            on, gain = hp.sidetone()
            g = hp.model.sidetone_gains[gain] if gain < len(hp.model.sidetone_gains) else gain
            return f"{'on' if on else 'off'}, gain {g}"

        rows = [
            ("Name", hp.name),
            ("Firmware", hp.firmware_version),
            ("Battery", lambda: f"{hp.battery()}%"),
            ("Codec setting", lambda: hp.codec().name),
            ("Streaming codec", lambda: getattr(hp.streaming_codec(), "name", "?")),
            ("Low latency", lambda: "on" if hp.low_latency() else "off"),
            ("EQ preset", preset),
            ("Sidetone", sidetone),
            ("Multipoint", lambda: "on" if hp.multipoint() else "off"),
            ("Voice prompts", lambda: getattr(hp.voice_prompt(), "name", "?")),
            ("Voice assistant", lambda: getattr(hp.voice_assistant(), "name", "?")),
            ("Volume step", hp.volume_step),
            ("Volume balance", hp.volume_balance),
        ]
        width = max(len(k) for k, _ in rows)
        for key, fn in rows:
            print(f"{key:<{width}}  {_try(fn)}")


def cmd_codec(args) -> None:
    with open_headphones(args) as hp:
        if args.value:
            hp.set_codec(Codec[args.value.upper()])
        print(hp.codec().name.lower())


def cmd_low_latency(args) -> None:
    with open_headphones(args) as hp:
        if args.value is not None:
            hp.set_low_latency(args.value)
        print("on" if hp.low_latency() else "off")


def cmd_sidetone(args) -> None:
    with open_headphones(args) as hp:
        if args.value is not None or args.gain:
            on, gain = hp.sidetone()
            if args.value is not None:
                on = args.value
            if args.gain:
                gain = [g.lower() for g in hp.model.sidetone_gains].index(args.gain)
            hp.set_sidetone(on, gain)
        on, gain = hp.sidetone()
        print(f"{'on' if on else 'off'} gain={hp.model.sidetone_gains[gain].lower()}")


def cmd_multipoint(args) -> None:
    with open_headphones(args) as hp:
        if args.value is not None:
            hp.set_multipoint(args.value)
        print("on" if hp.multipoint() else "off")


def _preset_key(name: str) -> str:
    return name.lower().replace(" ", "-")


def cmd_eq(args) -> None:
    with open_headphones(args) as hp:
        presets = {_preset_key(p.name): p for p in hp.model.eq_presets}
        v = args.value
        if v == "list":
            current = _try(hp.eq_bank)
            for key, p in presets.items():
                mark = "*" if str(p.bank) == current else " "
                print(f"{mark} {key}")
            return
        if v in ("on", "off"):
            hp.set_eq_enabled(v == "on")
        elif v:
            if v not in presets:
                sys.exit(f"unknown preset {v!r}; choose from: {', '.join(presets)}")
            hp.set_eq_bank(presets[v].bank)
        p = hp.eq_preset()
        state = "on" if hp.eq_enabled() else "off"
        print(f"{state} preset={_preset_key(p.name) if p else hp.eq_bank()}")


def cmd_name(args) -> None:
    with open_headphones(args) as hp:
        if args.value:
            hp.set_name(args.value)
        print(hp.name())


def cmd_voice_prompt(args) -> None:
    with open_headphones(args) as hp:
        if args.value:
            hp.set_voice_prompt(VoicePrompt[args.value.upper()])
        print(getattr(hp.voice_prompt(), "name", "?").lower())


def cmd_assistant(args) -> None:
    with open_headphones(args) as hp:
        if args.value:
            hp.set_voice_assistant(ASSISTANTS[args.value])
        va = hp.voice_assistant()
        print(next((k for k, v in ASSISTANTS.items() if v == va), va))


def cmd_balance(args) -> None:
    with open_headphones(args) as hp:
        if args.value is not None:
            hp.set_volume_balance(args.value)
        print(hp.volume_balance())


def cmd_volume_step(args) -> None:
    with open_headphones(args) as hp:
        if args.value is not None:
            hp.set_volume_step(args.value)
        print(hp.volume_step())


def cmd_battery(args) -> None:
    with open_headphones(args) as hp:
        print(hp.battery())


def cmd_pairing(args) -> None:
    with open_headphones(args) as hp:
        hp.enter_pairing_mode()
        print("pairing mode requested")


def _print_packet(pkt: Packet) -> None:
    if pkt.opcode == Op.AT_RECEIVE:
        msg = AtMessage.parse(pkt.params)
        if msg.is_notification:
            try:
                name = AtNotify(msg.sub_id).name
            except ValueError:
                name = str(msg.sub_id)
            print(f"notify {name}: {msg.payload.hex(' ')}", flush=True)
            return
    print(f"event {pkt}", flush=True)


def cmd_monitor(args) -> None:
    with open_headphones(args) as hp:
        stop = threading.Event()
        hp.client.on_packet.append(_print_packet)
        hp.client.on_disconnect.append(stop.set)
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        print("listening, Ctrl+C to stop", file=sys.stderr)
        stop.wait()


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(":", "").replace(" ", ""))


def cmd_raw(args) -> None:
    with open_headphones(args) as hp:
        hp.client.on_packet.append(_print_packet)
        expect = (lambda p: p.opcode == args.event) if args.event is not None else None
        pkt = hp.client.request(args.opcode, _hex(args.params), expect)
        print(f"ok {pkt}" if pkt else "acked")


def cmd_at(args) -> None:
    with open_headphones(args) as hp:
        print(hp.client.at(args.sub_id, _hex(args.payload)).hex(" ") or "(empty)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atconnect", description="Control Audio-Technica Bluetooth headphones.")
    p.add_argument("-d", "--device", help="Bluetooth address (default: first connected ATH-* device)")
    p.add_argument("--channel", type=int, help="RFCOMM channel to use instead of BlueZ profile lookup")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v: info, -vv: dump packets")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, takes_value=True, **value):
        sp = sub.add_parser(name, help=help_)
        if takes_value:
            sp.add_argument("value", nargs="?", **value)
        sp.set_defaults(func=fn)
        return sp

    sp = add("devices", cmd_devices, "list paired headphones", takes_value=False)
    sp.add_argument("-a", "--all", action="store_true", help="list all paired devices")
    add("status", cmd_status, "show all settings", takes_value=False)
    add("battery", cmd_battery, "battery level in percent", takes_value=False)
    add("codec", cmd_codec, "get/set preferred codec", choices=[c.name.lower() for c in Codec])
    add("low-latency", cmd_low_latency, "get/set low latency mode", type=_on_off)
    sp = add("sidetone", cmd_sidetone, "get/set sidetone (hear your voice during calls)", type=_on_off)
    sp.add_argument("--gain", choices=["low", "high"])
    add("multipoint", cmd_multipoint, "get/set multipoint", type=_on_off)
    add("eq", cmd_eq, "on | off | list | <preset>")
    add("name", cmd_name, "get/set Bluetooth name")
    add("voice-prompt", cmd_voice_prompt, "get/set voice guidance", choices=["voice", "beep"])
    add("assistant", cmd_assistant, "get/set voice assistant", choices=list(ASSISTANTS))
    add("balance", cmd_balance, "get/set L/R volume balance", type=int)
    add("volume-step", cmd_volume_step, "get/set volume step setting", type=int)
    add("pairing", cmd_pairing, "put headphones into pairing mode", takes_value=False)
    add("monitor", cmd_monitor, "print events sent by the headphones", takes_value=False)
    sp = sub.add_parser("raw", help="send a raw Realtek command (debug)")
    sp.add_argument("opcode", type=lambda s: int(s, 0))
    sp.add_argument("params", nargs="?", default="")
    sp.add_argument("--event", type=lambda s: int(s, 0), help="wait for this event opcode")
    sp.set_defaults(func=cmd_raw)
    sp = sub.add_parser("at", help="send a raw Audio-Technica vendor command (debug)")
    sp.add_argument("sub_id", type=lambda s: int(s, 0))
    sp.add_argument("payload", nargs="?", default="")
    sp.set_defaults(func=cmd_at)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(message)s")
    try:
        args.func(args)
    except (bluez.BluezError, CommandError, TimeoutError, ProtocolError, ValueError, OSError) as e:
        sys.exit(f"atconnect: {e}")


if __name__ == "__main__":
    main()
