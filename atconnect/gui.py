"""GTK 4 / libadwaita front end: `atconnect-gtk`."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import bluez  # noqa: E402
from .client import CommandError, RtkClient  # noqa: E402
from .device import Headphones, Unsupported  # noqa: E402
from .multipoint import Link, LinkTracker, describe_profiles  # noqa: E402
from .protocol import (  # noqa: E402
    AtMessage,
    AtNotify,
    BtProfile,
    Codec,
    LinkProfile,
    PairedDevice,
    ProtocolError,
    StreamingCodec,
    VoiceAssistant,
    VoicePrompt,
)

log = logging.getLogger(__name__)

APP_ID = "io.github.atconnect"
CODEC_LABELS = {Codec.AUTO: "Auto", Codec.LDAC: "LDAC", Codec.AAC: "AAC", Codec.SBC: "SBC"}
ASSISTANT_LABELS = {
    VoiceAssistant.DEFAULT: "Phone default",
    VoiceAssistant.AMA: "Amazon Alexa",
    VoiceAssistant.GAA: "Google Assistant",
}
PROMPT_LABELS = {VoicePrompt.VOICE: "Voice", VoicePrompt.BEEP: "Beep"}
CUSTOM = "custom"
BATTERY_POLL_S = 60
DEVICE_ERRORS = (CommandError, TimeoutError, OSError, ValueError, ProtocolError, Unsupported, bluez.BluezError)


def _combo(title: str, labels: list[str], subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle)
    row.set_model(Gtk.StringList.new(labels))
    return row


def _preset_label(key: str) -> str:
    return "Custom" if key == CUSTOM else key.replace("-", " ").title().replace("V Shaped", "V-shaped")


def _freq_label(f: float) -> str:
    return f"{f / 1000:g} kHz" if f >= 1000 else f"{f:g} Hz"


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Audio-Technica", default_width=480, default_height=780)
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atconnect-io")
        self._device: bluez.Device | None = None
        self._conn: bluez.Connection | None = None
        self._hp: Headphones | None = None
        self._tracker: LinkTracker | None = None
        self._names: dict[str, str] = {}
        self._updating = False  # suppresses change handlers while widgets are filled from the device
        self._battery_timer = 0
        self._expected_drop: str | None = None  # "pairing" / "handover" when we caused the disconnect
        self._presets: list[str] = []
        self._link_rows: dict[str, Adw.ActionRow] = {}
        self._paired_rows: list[Gtk.Widget] = []
        self._bluez_watch = 0

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Audio-Technica", subtitle="")
        header.set_title_widget(self.title)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reconnect")
        self.refresh_btn.connect("clicked", lambda *_: self.reconnect())
        header.pack_end(self.refresh_btn)
        view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.status_page = Adw.StatusPage(icon_name="audio-headphones-symbolic")
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        self.reclaim_btn = Gtk.Button(label="Connect to this computer", css_classes=["pill", "suggested-action"])
        self.reclaim_btn.connect("clicked", lambda *_: self._reclaim())
        retry = Gtk.Button(label="Try again", css_classes=["pill"])
        retry.connect("clicked", lambda *_: self.reconnect())
        status_box.append(self.reclaim_btn)
        status_box.append(retry)
        self.status_page.set_child(status_box)
        self.stack.add_named(self.status_page, "status")
        spinner_page = Adw.StatusPage(title="Connecting…", child=Adw.Spinner(width_request=32, height_request=32))
        self.stack.add_named(spinner_page, "connecting")
        self.stack.add_named(self._build_settings(), "settings")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.connect("close-request", self._on_close)
        self._watch_bluez()

    # ---- layout --------------------------------------------------------

    def _build_settings(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        info = Adw.PreferencesGroup()
        self.battery_row = Adw.ActionRow(title="Battery")
        self.battery_bar = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER, width_request=120)
        self.battery_label = Gtk.Label(css_classes=["numeric", "dim-label"])
        self.battery_row.add_suffix(self.battery_label)
        self.battery_row.add_suffix(self.battery_bar)
        self.battery_row.add_prefix(Gtk.Image(icon_name="battery-full-symbolic"))
        self.streaming_row = Adw.ActionRow(title="Now playing with")
        self.streaming_label = Gtk.Label(css_classes=["dim-label"])
        self.streaming_row.add_suffix(self.streaming_label)
        self.firmware_row = Adw.ActionRow(title="Firmware")
        self.firmware_label = Gtk.Label(css_classes=["dim-label", "numeric"])
        self.firmware_row.add_suffix(self.firmware_label)
        for w in (self.battery_row, self.streaming_row, self.firmware_row):
            info.add(w)
        page.add(info)

        sound = Adw.PreferencesGroup(title="Sound")
        self.eq_switch = Adw.SwitchRow(title="Equalizer", subtitle="Audio restarts briefly when toggled")
        self.eq_switch.connect("notify::active", self._on_eq_switch)
        self.eq_row = _combo("Preset", [])
        self.eq_row.connect("notify::selected", self._on_eq_preset)
        self.eq_custom = Adw.ExpanderRow(title="Custom curve", subtitle="Gain per band, −12…+12 dB")
        self.eq_scales: list[Gtk.Scale] = []
        self.eq_apply = Adw.ButtonRow(title="Apply custom curve", start_icon_name="object-select-symbolic")
        self.eq_apply.connect("activated", self._on_eq_apply)
        self.codec_row = _combo(
            "Preferred codec", list(CODEC_LABELS.values()), "Applied after the headphones reconnect"
        )
        self.codec_row.connect("notify::selected", self._on_codec)
        self.latency_row = Adw.SwitchRow(title="Low latency mode", subtitle="Requires the AAC or SBC codec")
        self.latency_row.connect("notify::active", self._on_latency)
        for w in (self.eq_switch, self.eq_row, self.eq_custom, self.codec_row, self.latency_row):
            sound.add(w)
        page.add(sound)

        mp = Adw.PreferencesGroup(title="Multipoint")
        self.multipoint_row = Adw.SwitchRow(title="Multipoint", subtitle="Stay connected to two devices at once")
        self.multipoint_row.connect("notify::active", self._on_multipoint)
        mp.add(self.multipoint_row)
        self.mp_group = mp
        page.add(mp)

        self.links_group = Adw.PreferencesGroup(
            title="Connected devices",
            description="Other devices show up as they connect or disconnect.",
        )
        page.add(self.links_group)

        self.paired_group = Adw.PreferencesGroup(title="Paired devices", visible=False)
        page.add(self.paired_group)

        actions = Adw.PreferencesGroup()
        pair = Adw.ButtonRow(title="Pair another device…", start_icon_name="list-add-symbolic")
        pair.connect("activated", self._on_pair)
        last = Adw.ButtonRow(title="Reconnect last used device", start_icon_name="emblem-synchronizing-symbolic")
        last.connect("activated", self._on_reconnect_last)
        handover = Adw.ButtonRow(title="Hand over to the other device…", start_icon_name="go-next-symbolic")
        handover.connect("activated", self._on_handover)
        for w in (pair, last, handover):
            actions.add(w)
        page.add(actions)

        this = Adw.PreferencesGroup(title="This computer")
        self.a2dp_row = Adw.SwitchRow(title="Audio", subtitle="Play music from this computer (A2DP)", active=True)
        self.a2dp_row.connect("notify::active", self._on_profile, "a2dp")
        self.hfp_row = Adw.SwitchRow(title="Calls", subtitle="Use the microphone with this computer (HFP)",
                                     active=True)
        self.hfp_row.connect("notify::active", self._on_profile, "hfp")
        this.add(self.a2dp_row)
        this.add(self.hfp_row)
        page.add(this)

        calls = Adw.PreferencesGroup(title="Calls")
        self.sidetone_row = Adw.SwitchRow(title="Sidetone", subtitle="Hear your own voice during calls")
        self.sidetone_row.connect("notify::active", self._on_sidetone)
        self.sidetone_gain_row = _combo("Sidetone level", [])
        self.sidetone_gain_row.connect("notify::selected", self._on_sidetone)
        calls.add(self.sidetone_row)
        calls.add(self.sidetone_gain_row)
        page.add(calls)

        dev = Adw.PreferencesGroup(title="Device")
        self.prompt_row = _combo("Voice guidance", list(PROMPT_LABELS.values()))
        self.prompt_row.connect("notify::selected", self._on_prompt)
        self.assistant_row = _combo("Voice assistant", list(ASSISTANT_LABELS.values()))
        self.assistant_row.connect("notify::selected", self._on_assistant)
        self.name_row = Adw.EntryRow(title="Bluetooth name", show_apply_button=True)
        self.name_row.connect("apply", self._on_name)
        for w in (self.prompt_row, self.assistant_row, self.name_row):
            dev.add(w)
        page.add(dev)
        return page

    def _build_eq_bands(self, freqs: tuple[float, ...]) -> None:
        if len(self.eq_scales) == len(freqs):
            return
        for f in freqs:
            row = Adw.ActionRow(title=_freq_label(f))
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -12, 12, 0.5)
            scale.set_hexpand(True)
            scale.add_css_class("numeric")
            scale.set_size_request(220, -1)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.LEFT)
            scale.set_format_value_func(lambda _s, v: f"{v:+.1f} dB")
            scale.add_mark(0, Gtk.PositionType.BOTTOM, None)
            row.add_suffix(scale)
            row.set_activatable_widget(scale)
            self.eq_custom.add_row(row)
            self.eq_scales.append(scale)
        if not self.eq_apply.get_parent():
            self.eq_custom.add_row(self.eq_apply)

    # ---- helpers -------------------------------------------------------

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def run(self, fn: Callable[[Headphones], object], done: Callable[[object], None] | None = None,
            what: str = "") -> None:
        """Run a device call on the I/O thread and deliver its result in the main loop."""
        hp = self._hp
        if hp is None:
            return

        def job():
            try:
                res = fn(hp)
            except DEVICE_ERRORS as e:
                log.warning("%s failed: %s", what or fn, e)
                GLib.idle_add(self._on_error, what, e)
                return
            if done:
                GLib.idle_add(self._deliver, done, res)

        self._io.submit(job)

    def run_bluez(self, fn: Callable[[], object], done: Callable[[object], None] | None = None,
                  what: str = "") -> None:
        def job():
            try:
                res = fn()
            except DEVICE_ERRORS as e:
                GLib.idle_add(self._on_error, what, e)
                return
            if done:
                GLib.idle_add(self._deliver, done, res)

        self._io.submit(job)

    def _deliver(self, done, res) -> bool:
        self._updating = True
        try:
            done(res)
        finally:
            self._updating = False
        return False

    def _on_error(self, what: str, err: Exception) -> bool:
        if self._hp and not self._hp.client.connected:
            self._on_device_lost(self._hp.client)
        elif what:
            self.toast(f"Could not {what}: {err}")
        return False

    # ---- connection ----------------------------------------------------

    def reconnect(self) -> None:
        self._teardown()
        try:
            device = bluez.find_device()
        except (bluez.BluezError, GLib.Error) as e:
            self._show_disconnected("Headphones not connected", str(e))
            return
        self._device = device
        self.stack.set_visible_child_name("connecting")
        self.title.set_title(device.name.removeprefix("LE_"))
        self.title.set_subtitle(device.address)
        bluez.connect_async(device, self._on_connected)

    def _on_connected(self, conn: bluez.Connection | None, err: str | None) -> None:
        if conn is None:
            self._show_disconnected("Headphones not connected", err or "Connection failed")
            return
        self._conn = conn
        self._expected_drop = None
        client = RtkClient(conn.sock)
        client.on_at_notification.append(lambda m: GLib.idle_add(self._on_notification, m))
        client.on_disconnect.append(lambda: GLib.idle_add(self._on_device_lost, client))
        self._hp = Headphones(client)
        try:
            self._names = bluez.known_device_names()
            own = bluez.adapter_address()
        except GLib.Error:
            own = None
        self._tracker = LinkTracker(client, own)
        self._tracker.on_change.append(lambda ev, link: GLib.idle_add(self._on_link_change, link))
        model = self._hp.model
        self._presets = list(model.eq_presets) + [CUSTOM]
        # Replacing a model resets the selection; don't let that be written to the device.
        self._updating = True
        try:
            self.eq_row.set_model(Gtk.StringList.new([_preset_label(p) for p in self._presets]))
            self.sidetone_gain_row.set_model(Gtk.StringList.new(list(model.sidetone_gains)))
            self._build_eq_bands(model.eq_freqs)
            self.a2dp_row.set_active(True)
            self.hfp_row.set_active(True)
        finally:
            self._updating = False
        self._render_links()
        self.stack.set_visible_child_name("settings")
        self.load_all()
        self._battery_timer = GLib.timeout_add_seconds(BATTERY_POLL_S, self._poll_battery)

    def _on_device_lost(self, client: RtkClient) -> bool:
        if not (self._hp and self._hp.client is client):
            return False
        if self._expected_drop == "pairing":
            self._show_disconnected(
                "Pairing mode",
                "The headphones are discoverable now. Pair your other device, "
                "then connect them back to this computer.",
                icon="bluetooth-symbolic",
            )
        elif self._expected_drop == "handover":
            self._show_disconnected(
                "Handed over",
                "This computer let go of the headphones so your other device can use them.",
                icon="go-next-symbolic",
            )
        else:
            self._show_disconnected("Headphones disconnected", "")
        return False

    def _show_disconnected(self, title: str, reason: str, icon: str = "audio-headphones-symbolic") -> None:
        self._teardown()
        self.status_page.set_icon_name(icon)
        self.status_page.set_title(title)
        self.status_page.set_description(reason)
        self.reclaim_btn.set_visible(self._device is not None)
        self.title.set_subtitle("")
        self.stack.set_visible_child_name("status")

    def _teardown(self) -> None:
        if self._battery_timer:
            GLib.source_remove(self._battery_timer)
            self._battery_timer = 0
        if self._hp:
            self._hp.client.close()
            self._hp = None
        if self._conn:
            self._conn.close()
            self._conn = None
        self._tracker = None

    def _reclaim(self) -> None:
        device = self._device
        if device is None:
            self.reconnect()
            return
        self.stack.set_visible_child_name("connecting")
        self.run_bluez(lambda: bluez.connect_device(device), lambda _: self.reconnect(),
                       what="connect the headphones")

    def _watch_bluez(self) -> None:
        """Reconnect automatically when the headphones come back to this computer."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            return

        def on_props(_conn, _sender, path, _iface, _signal, params):
            iface, changed, _ = params.unpack()
            if iface != "org.bluez.Device1" or not changed.get("Connected"):
                return
            if self._device and path == self._device.path and self._hp is None \
                    and self.stack.get_visible_child_name() == "status":
                GLib.timeout_add_seconds(2, lambda: (self.reconnect(), False)[1])

        self._bluez_watch = bus.signal_subscribe(
            "org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged", None, None,
            Gio.DBusSignalFlags.NONE, on_props,
        )

    def _on_close(self, *_args) -> bool:
        self._teardown()
        self._io.shutdown(wait=False, cancel_futures=True)
        return False

    # ---- reading state -------------------------------------------------

    def load_all(self) -> None:
        model = self._hp.model
        self.run(lambda hp: hp.name(), self._set_name)
        self.run(lambda hp: hp.battery(), self._set_battery)
        self.run(lambda hp: hp.firmware_version(), self.firmware_label.set_label)
        self.run(lambda hp: hp.streaming_codec(), self._set_streaming)
        self.run(lambda hp: hp.eq_enabled(), self.eq_switch.set_active)
        self.run(lambda hp: (hp.eq_preset(), hp.eq_curve()), self._set_eq)
        self.run(lambda hp: hp.codec(), self._set_codec)
        self.run(lambda hp: hp.low_latency(), self.latency_row.set_active)
        self.run(lambda hp: hp.sidetone(), self._set_sidetone)
        self.run(lambda hp: hp.multipoint(), self.multipoint_row.set_active)
        self.run(lambda hp: hp.voice_prompt(), lambda v: self._select(self.prompt_row, list(PROMPT_LABELS), v))
        self.run(
            lambda hp: hp.voice_assistant(),
            lambda v: self._select(self.assistant_row, list(ASSISTANT_LABELS), v),
        )
        self.run(lambda hp: hp.multipoint_management_supported(), self._set_management)
        self.codec_row.set_sensitive(len(model.codecs) > 1)

    @staticmethod
    def _select(row: Adw.ComboRow, values: list, value) -> None:
        if value in values:
            row.set_selected(values.index(value))

    def _set_name(self, name: str) -> None:
        self.name_row.set_text(name)
        self.title.set_title(name)

    def _set_battery(self, level: int) -> None:
        self.battery_bar.set_value(level)
        self.battery_label.set_label(f"{level}%")
        self.title.set_subtitle(f"Battery {level}%")

    def _set_streaming(self, codec) -> None:
        name = getattr(codec, "name", str(codec))
        self.streaming_label.set_label("Nothing" if name == "NONE" else name.replace("_", " "))

    def _set_codec(self, codec: Codec) -> None:
        self._select(self.codec_row, list(CODEC_LABELS), codec)
        self.latency_row.set_sensitive(codec not in self._hp.model.low_latency_unsupported)

    def _set_eq(self, value) -> None:
        preset, (_, stages) = value
        self._select(self.eq_row, self._presets, preset or CUSTOM)
        for scale, (gain, _, _) in zip(self.eq_scales, stages):
            scale.set_value(round(gain * 2) / 2)
        self.eq_custom.set_expanded(preset == CUSTOM)

    def _set_sidetone(self, value: tuple[bool, int]) -> None:
        on, gain = value
        self.sidetone_row.set_active(on)
        self.sidetone_gain_row.set_selected(gain)
        self.sidetone_gain_row.set_sensitive(on)

    def _poll_battery(self) -> bool:
        self.run(lambda hp: hp.battery(), self._set_battery)
        return True

    def _on_notification(self, msg: AtMessage) -> bool:
        self._updating = True
        try:
            if msg.sub_id == AtNotify.STREAMING_CODEC and msg.payload:
                try:
                    self._set_streaming(StreamingCodec(msg.payload[0]))
                except ValueError:
                    pass
            elif msg.sub_id == AtNotify.LOW_LATENCY_MODE and msg.payload:
                self.latency_row.set_active(msg.payload[0] != 0)
            elif msg.sub_id == AtNotify.EQ_STATE and msg.payload:
                self.eq_switch.set_active(msg.payload[0] != 0)
        finally:
            self._updating = False
        return False

    # ---- multipoint rendering ---------------------------------------------

    def _on_link_change(self, link: Link) -> bool:
        self._render_links()
        if link.is_this_computer:
            self._updating = True
            try:
                if LinkProfile.A2DP in link.profiles or not link.profiles:
                    self.a2dp_row.set_active(LinkProfile.A2DP in link.profiles)
            finally:
                self._updating = False
        else:
            name = self._names.get(link.address, link.address)
            self.toast(f"{name} {'connected' if link.connected else 'disconnected'}")
        if self._hp and self._hp.multipoint_management_supported():
            self.run(lambda hp: hp.paired_devices(), self._render_paired)
        return False

    def _render_links(self) -> None:
        for row in self._link_rows.values():
            self.links_group.remove(row)
        self._link_rows = {}
        if not self._tracker:
            return
        for link in self._tracker.links:
            if link.is_this_computer:
                title, icon = "This computer", "computer-symbolic"
            else:
                title, icon = self._names.get(link.address) or "Other device", "phone-symbolic"
            state = describe_profiles(link.profiles & ~LinkProfile.SPP) if link.connected else "disconnected"
            if link.is_this_computer and link.profiles == LinkProfile.SPP:
                state = "connected"
            row = Adw.ActionRow(title=title, subtitle=f"{link.address} · {state}")
            row.add_prefix(Gtk.Image(icon_name=icon))
            self.links_group.add(row)
            self._link_rows[link.address] = row

    def _set_management(self, supported: bool) -> None:
        self.paired_group.set_visible(supported)
        if supported:
            self.run(lambda hp: hp.paired_devices(), self._render_paired)

    def _render_paired(self, devices: list[PairedDevice]) -> None:
        for row in self._paired_rows:
            self.paired_group.remove(row)
        self._paired_rows = []
        for d in devices:
            name = d.name or self._names.get(d.address) or d.address
            profiles = "+".join(p.name for p in (BtProfile.A2DP, BtProfile.HFP) if p in d.connected)
            state = ("active · " if d.active else "") + (f"connected ({profiles})" if d.is_connected
                                                         else "disconnected")
            row = Adw.ActionRow(title=name, subtitle=f"{d.address} · {state}")
            toggle = Gtk.Button(
                icon_name="network-offline-symbolic" if d.is_connected else "network-transmit-receive-symbolic",
                tooltip_text="Disconnect" if d.is_connected else "Connect",
                valign=Gtk.Align.CENTER, css_classes=["flat"],
            )
            toggle.connect("clicked", self._on_paired_toggle, d)
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Forget",
                                valign=Gtk.Align.CENTER, css_classes=["flat"])
            remove.connect("clicked", self._on_paired_remove, d)
            row.add_suffix(toggle)
            row.add_suffix(remove)
            self.paired_group.add(row)
            self._paired_rows.append(row)

    # ---- user changes --------------------------------------------------

    def _on_eq_switch(self, row, _pspec) -> None:
        if not self._updating:
            on = row.get_active()
            self.run(lambda hp: hp.set_eq_enabled(on), what="toggle the equalizer")

    def _on_eq_preset(self, row, _pspec) -> None:
        if self._updating or not self._hp or row.get_selected() >= len(self._presets):
            return
        key = self._presets[row.get_selected()]
        if key == CUSTOM:
            self.eq_custom.set_expanded(True)
            return
        self.eq_custom.set_expanded(False)

        def apply(hp: Headphones):
            hp.set_eq_preset(key)
            return hp.eq_preset(), hp.eq_curve()

        self.run(apply, self._after_eq_write, what="change the equalizer")

    def _on_eq_apply(self, _row) -> None:
        gains = [s.get_value() for s in self.eq_scales]

        def apply(hp: Headphones):
            hp.set_eq_gains(gains)
            return hp.eq_preset(), hp.eq_curve()

        self.run(apply, self._after_eq_write, what="apply the custom curve")

    def _after_eq_write(self, value) -> None:
        self._set_eq(value)
        if not self.eq_switch.get_active():
            self.toast("Saved. Turn the equalizer on to hear it")

    def _on_codec(self, row, _pspec) -> None:
        if self._updating:
            return
        codec = list(CODEC_LABELS)[row.get_selected()]
        self.latency_row.set_sensitive(codec not in self._hp.model.low_latency_unsupported)
        self.run(lambda hp: hp.set_codec(codec), lambda _: self.toast("Reconnect the headphones to apply"),
                 what="change the codec")

    def _on_latency(self, row, _pspec) -> None:
        if not self._updating:
            on = row.get_active()
            self.run(lambda hp: hp.set_low_latency(on), what="change low latency mode")

    def _on_sidetone(self, *_args) -> None:
        on = self.sidetone_row.get_active()
        self.sidetone_gain_row.set_sensitive(on)
        if not self._updating:
            gain = self.sidetone_gain_row.get_selected()
            self.run(lambda hp: hp.set_sidetone(on, gain), what="change sidetone")

    def _on_multipoint(self, row, _pspec) -> None:
        if not self._updating:
            on = row.get_active()
            self.run(lambda hp: hp.set_multipoint(on), what="change multipoint")

    def _confirm(self, heading: str, body: str, action: str, callback: Callable[[], None]) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", action)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.connect("response", lambda _d, r: callback() if r == "ok" else None)
        dialog.present(self)

    def _on_pair(self, _row) -> None:
        def go():
            self._expected_drop = "pairing"

            def started(kept_link: bool) -> None:
                if kept_link:
                    self._expected_drop = None
                    self.toast("Pairing mode on. Pair your other device now")

            self.run(lambda hp: hp.start_pairing(), started, what="enter pairing mode")

        managed = self._hp is not None and self._hp._mp_management
        body = (
            "The headphones become discoverable so you can pair a phone or another computer."
            if managed else
            "The headphones will disconnect from this computer and become discoverable. Pair your "
            "other device, then press “Connect to this computer” to get them back — with multipoint "
            "on, both devices stay connected."
        )
        self._confirm("Pair another device?", body, "Start pairing", go)

    def _on_reconnect_last(self, _row) -> None:
        self.run(lambda hp: hp.reconnect_last_device(),
                 lambda _: self.toast("Asked the headphones to link back to the last device"),
                 what="reconnect the last device")

    def _on_handover(self, _row) -> None:
        device = self._device
        if device is None:
            return

        def go():
            self._expected_drop = "handover"
            self.run_bluez(lambda: bluez.disconnect_device(device), what="disconnect this computer")

        self._confirm(
            "Hand over the headphones?",
            "This computer disconnects so your phone or other device can use the headphones alone. "
            "You can connect them back from here at any time.",
            "Hand over", go,
        )

    def _on_profile(self, row, _pspec, profile: str) -> None:
        if self._updating or self._device is None:
            return
        on = row.get_active()
        device = self._device
        self.run_bluez(lambda: bluez.set_profile(device, profile, on),
                       what=f"{'connect' if on else 'disconnect'} {profile.upper()}")

    def _on_paired_toggle(self, _btn, d: PairedDevice) -> None:
        fn = Headphones.disconnect_device if d.is_connected else Headphones.connect_device
        self.run(lambda hp: (fn(hp, d.address), hp.paired_devices())[1], self._render_paired,
                 what="change the connection")

    def _on_paired_remove(self, _btn, d: PairedDevice) -> None:
        def go():
            self.run(lambda hp: (hp.remove_device(d.address), hp.paired_devices())[1], self._render_paired,
                     what="forget the device")

        self._confirm(f"Forget {d.name or d.address}?",
                      "The headphones will no longer connect to this device automatically.", "Forget", go)

    def _on_prompt(self, row, _pspec) -> None:
        if not self._updating:
            vp = list(PROMPT_LABELS)[row.get_selected()]
            self.run(lambda hp: hp.set_voice_prompt(vp), what="change voice guidance")

    def _on_assistant(self, row, _pspec) -> None:
        if not self._updating:
            va = list(ASSISTANT_LABELS)[row.get_selected()]
            self.run(lambda hp: hp.set_voice_assistant(va), what="change the voice assistant")

    def _on_name(self, row) -> None:
        name = row.get_text().strip()
        self.run(lambda hp: (hp.set_name(name), hp.name())[1], self._set_name, what="rename the headphones")


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: Window | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = Window(self)
            self.window.reconnect()
        self.window.present()


def main() -> None:
    logging.basicConfig(level=logging.INFO if "-v" in sys.argv else logging.WARNING)
    sys.exit(App().run([a for a in sys.argv if a != "-v"]))


if __name__ == "__main__":
    main()
