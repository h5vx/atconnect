"""Command line interface: `atconnect --help`."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from . import bluez
from .client import CommandError, RtkClient
from .device import Headphones, Unsupported
from .multipoint import LinkTracker, describe_profiles
from .protocol import (
    AtMessage,
    AtNotify,
    BtProfile,
    Codec,
    Op,
    Packet,
    ProtocolError,
    VoiceAssistant,
    VoicePrompt,
    parse_link_event,
)

ON_OFF = {"on": True, "off": False, "1": True, "0": False, "true": True, "false": False}
ASSISTANTS = {"default": VoiceAssistant.DEFAULT, "alexa": VoiceAssistant.AMA, "google": VoiceAssistant.GAA}
ERRORS = (bluez.BluezError, CommandError, TimeoutError, ProtocolError, ValueError, OSError, Unsupported)


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
    except (CommandError, TimeoutError, ProtocolError, ValueError, Unsupported) as e:
        return f"n/a ({e})"


def _on(v: bool) -> str:
    return "on" if v else "off"


def cmd_devices(args) -> None:
    devices = bluez.list_devices(only_supported=not args.all)
    if not devices:
        print("no paired Audio-Technica headphones")
    for d in devices:
        mark = "*" if d.has_vendor_service else " "
        print(f"{mark} {d}")


def cmd_status(args) -> None:
    with open_headphones(args) as hp:
        def sidetone():
            on, gain = hp.sidetone()
            g = hp.model.sidetone_gains[gain] if gain < len(hp.model.sidetone_gains) else gain
            return f"{_on(on)}, gain {g}"

        rows = [
            ("Name", hp.name),
            ("Firmware", hp.firmware_version),
            ("Battery", lambda: f"{hp.battery()}%"),
            ("Codec setting", lambda: hp.codec().name),
            ("Streaming codec", lambda: getattr(hp.streaming_codec(), "name", "?")),
            ("Low latency", lambda: _on(hp.low_latency())),
            ("Equalizer", lambda: f"{_on(hp.eq_enabled())}, {hp.eq_preset() or 'other bank'}"),
            ("Sidetone", sidetone),
            ("Multipoint", lambda: _on(hp.multipoint())),
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
        print(_on(hp.low_latency()))


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
        print(f"{_on(on)} gain={hp.model.sidetone_gains[gain].lower()}")


# ---- equalizer ---------------------------------------------------------------


def _print_eq(hp: Headphones) -> None:
    print(f"equalizer: {_on(hp.eq_enabled())}")
    print(f"preset:    {hp.eq_preset() or 'other bank selected'}")
    global_gain, stages = hp.eq_curve()
    print(f"curve:     global {global_gain:+.1f} dB")
    for gain, freq, q in stages:
        print(f"           {freq:7.0f} Hz  {gain:+5.1f} dB  Q {q:.2f}")


def cmd_eq(args) -> None:
    with open_headphones(args) as hp:
        action = args.action
        if action == "list":
            current = _try(hp.eq_preset)
            for name in hp.model.eq_presets:
                print(f"{'*' if name == current else ' '} {name}")
            return
        if action in ("on", "off"):
            hp.set_eq_enabled(action == "on")
            print("note: the headphones briefly restart the audio stream when the EQ is toggled",
                  file=sys.stderr)
        elif action == "preset":
            if not args.values:
                sys.exit("usage: atconnect eq preset NAME")
            hp.set_eq_preset(args.values[0])
        elif action == "set":
            freqs = hp.model.eq_freqs
            if len(args.values) != len(freqs):
                bands = " ".join(f"{f:g}Hz" for f in freqs)
                sys.exit(f"usage: atconnect eq set G1..G{len(freqs)}  (dB for {bands})")
            hp.set_eq_gains([float(v) for v in args.values], args.global_gain)
        if action in ("preset", "set") and not hp.eq_enabled():
            print("note: the equalizer is off; enable it with `atconnect eq on`", file=sys.stderr)
        _print_eq(hp)


# ---- multipoint ----------------------------------------------------------------


def _handover_reconnect(dev: bluez.Device, seconds: float) -> None:
    print(f"reconnecting this computer in {seconds:g}s …", file=sys.stderr)
    time.sleep(seconds)
    bluez.connect_device(dev)
    print("reconnected", file=sys.stderr)


def cmd_multipoint(args) -> None:
    action = args.action
    dev = bluez.find_device(args.device)

    if action == "handover":
        bluez.disconnect_device(dev)
        print(f"{dev.name} disconnected from this computer; your other device can take over")
        return
    if action == "reclaim":
        bluez.connect_device(dev)
        print(f"{dev.name} connected to this computer")
        return

    with open_headphones(args) as hp:
        if action in ("on", "off"):
            hp.set_multipoint(action == "on")
        elif action == "pair":
            kept = hp.start_pairing()
            if kept:
                print("pairing mode on; pair your other device now (`atconnect multipoint cancel` to stop)")
                return
            print("pairing mode on: the headphones dropped this computer to become discoverable.")
            print("Pair your other device now.")
            if args.reconnect:
                hp.client.close()
                _handover_reconnect(dev, args.reconnect)
            else:
                print("Afterwards run `atconnect multipoint reclaim` to reconnect this computer.")
            return
        elif action == "cancel":
            hp.cancel_pairing()
        elif action == "reconnect-last":
            hp.reconnect_last_device()
            print("asked the headphones to link back to the last used device")
            return
        elif action == "devices":
            names = bluez.known_device_names()
            own = _try(hp.own_address)
            for d in hp.paired_devices():
                state = "connected " + describe_bt(d.connected) if d.is_connected else "disconnected"
                flags = " active" if d.active else ""
                me = " (this computer)" if d.address == own else ""
                name = d.name or names.get(d.address, "")
                print(f"{d.address}  {name}{me}  {state}{flags}  supports {describe_bt(d.supported)}")
            return
        elif action in ("connect", "disconnect", "remove"):
            if not args.address:
                sys.exit(f"usage: atconnect multipoint {action} ADDRESS")
            {"connect": hp.connect_device, "disconnect": hp.disconnect_device,
             "remove": hp.remove_device}[action](args.address)
            print("done")
            return
        elif action == "profile":
            if not args.address or args.profile is None or args.state is None:
                sys.exit("usage: atconnect multipoint profile ADDRESS a2dp|hfp on|off")
            hp.set_device_profile(args.address, BtProfile[args.profile.upper()], args.state)
            print("done")
            return
        elif action == "watch":
            _watch_links(hp)
            return

        print(f"multipoint:        {_on(hp.multipoint())}")
        supported = hp.multipoint_management_supported()
        print(f"device management: {'supported' if supported else 'not supported by this firmware'}")
        if not supported:
            print("available: on/off, pair, cancel, reconnect-last, watch, handover, reclaim")


def describe_bt(p: BtProfile) -> str:
    return "+".join(x.name for x in (BtProfile.A2DP, BtProfile.HFP) if x in p) or "none"


def _watch_links(hp: Headphones) -> None:
    names = bluez.known_device_names()
    tracker = LinkTracker(hp.client, bluez.adapter_address())
    stop = threading.Event()

    def show(ev, link):
        who = "this computer" if link.is_this_computer else names.get(link.address, "unknown device")
        verb = "connected" if ev.connected else "disconnected"
        print(f"{time.strftime('%H:%M:%S')}  {link.address} ({who}) {verb} {describe_profiles(ev.profile)}"
              f" -> now {describe_profiles(link.profiles)}", flush=True)

    tracker.on_change.append(show)
    hp.client.on_disconnect.append(stop.set)
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print("watching device links, Ctrl+C to stop", file=sys.stderr)
    stop.wait()


def cmd_link(args) -> None:
    dev = bluez.find_device(args.device)
    bluez.set_profile(dev, args.profile, args.state)
    print(f"{args.profile} {_on(args.state)} for this computer")


# ---- misc ------------------------------------------------------------------------


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


def cmd_firmware(args) -> None:
    with open_headphones(args) as hp:
        print(hp.firmware_version())


def _print_packet(pkt: Packet) -> None:
    link = parse_link_event(pkt)
    if link:
        verb = "connected" if link.connected else "disconnected"
        print(f"link {link.address} {verb} {describe_profiles(link.profile)}", flush=True)
        return
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
    add("firmware", cmd_firmware, "firmware version", takes_value=False)
    add("codec", cmd_codec, "get/set preferred codec", choices=[c.name.lower() for c in Codec])
    add("low-latency", cmd_low_latency, "get/set low latency mode", type=_on_off)
    sp = add("sidetone", cmd_sidetone, "get/set sidetone (hear your voice during calls)", type=_on_off)
    sp.add_argument("--gain", choices=["low", "high"])

    sp = sub.add_parser("eq", help="equalizer: show | on | off | list | preset NAME | set G1..G5")
    sp.add_argument("action", nargs="?", default="show", choices=["show", "on", "off", "list", "preset", "set"])
    sp.add_argument("values", nargs="*")
    sp.add_argument("--global-gain", type=float, default=-6.0, help="pre-gain in dB for `set` (default -6)")
    sp.set_defaults(func=cmd_eq)

    sp = sub.add_parser("multipoint", help="multipoint: status | on | off | pair | cancel | reconnect-last | "
                                            "watch | handover | reclaim | devices | connect | disconnect | "
                                            "remove | profile")
    sp.add_argument("action", nargs="?", default="status",
                    choices=["status", "on", "off", "pair", "cancel", "reconnect-last", "watch", "handover",
                             "reclaim", "devices", "connect", "disconnect", "remove", "profile"])
    sp.add_argument("address", nargs="?")
    sp.add_argument("profile", nargs="?", choices=["a2dp", "hfp"])
    sp.add_argument("state", nargs="?", type=_on_off)
    sp.add_argument("--reconnect", type=float, metavar="SECONDS",
                    help="with `pair`: reconnect this computer after SECONDS")
    sp.set_defaults(func=cmd_multipoint)

    sp = sub.add_parser("link", help="connect/disconnect a profile between this computer and the headphones")
    sp.add_argument("profile", choices=["a2dp", "hfp"])
    sp.add_argument("state", type=_on_off)
    sp.set_defaults(func=cmd_link)

    add("name", cmd_name, "get/set Bluetooth name")
    add("voice-prompt", cmd_voice_prompt, "get/set voice guidance", choices=["voice", "beep"])
    add("assistant", cmd_assistant, "get/set voice assistant", choices=list(ASSISTANTS))
    add("balance", cmd_balance, "get/set L/R volume balance", type=int)
    add("volume-step", cmd_volume_step, "get/set volume step setting", type=int)
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
    except ERRORS as e:
        sys.exit(f"atconnect: {e}")


if __name__ == "__main__":
    main()
