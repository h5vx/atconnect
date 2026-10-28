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
from .device import Headphones  # noqa: E402
from .protocol import AtMessage, AtNotify, Codec, StreamingCodec, VoiceAssistant, VoicePrompt  # noqa: E402

log = logging.getLogger(__name__)

APP_ID = "io.github.atconnect"
CODEC_LABELS = {Codec.AUTO: "Auto", Codec.LDAC: "LDAC", Codec.AAC: "AAC", Codec.SBC: "SBC"}
ASSISTANT_LABELS = {
    VoiceAssistant.DEFAULT: "Phone default",
    VoiceAssistant.AMA: "Amazon Alexa",
    VoiceAssistant.GAA: "Google Assistant",
}
PROMPT_LABELS = {VoicePrompt.VOICE: "Voice", VoicePrompt.BEEP: "Beep"}
BATTERY_POLL_S = 60


def _combo(title: str, labels: list[str], subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle)
    row.set_model(Gtk.StringList.new(labels))
    return row


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Audio-Technica", default_width=460, default_height=720)
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atconnect-io")
        self._conn: bluez.Connection | None = None
        self._hp: Headphones | None = None
        self._updating = False  # suppresses change handlers while widgets are filled from the device
        self._battery_timer = 0

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
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda *_: self.reconnect())
        self.status_page.set_child(retry)
        self.stack.add_named(self.status_page, "status")
        spinner_page = Adw.StatusPage(title="Connecting…", child=Adw.Spinner(width_request=32, height_request=32))
        self.stack.add_named(spinner_page, "connecting")
        self.stack.add_named(self._build_settings(), "settings")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.connect("close-request", self._on_close)

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
        info.add(self.battery_row)
        info.add(self.streaming_row)
        page.add(info)

        sound = Adw.PreferencesGroup(title="Sound")
        self.eq_row = _combo("Equalizer", [])
        self.eq_row.connect("notify::selected", self._on_eq)
        self.codec_row = _combo(
            "Preferred codec", list(CODEC_LABELS.values()), "Applied after the headphones reconnect"
        )
        self.codec_row.connect("notify::selected", self._on_codec)
        self.latency_row = Adw.SwitchRow(title="Low latency mode", subtitle="Requires the AAC or SBC codec")
        self.latency_row.connect("notify::active", self._on_latency)
        for w in (self.eq_row, self.codec_row, self.latency_row):
            sound.add(w)
        page.add(sound)

        calls = Adw.PreferencesGroup(title="Calls")
        self.sidetone_row = Adw.SwitchRow(title="Sidetone", subtitle="Hear your own voice during calls")
        self.sidetone_row.connect("notify::active", self._on_sidetone)
        self.sidetone_gain_row = _combo("Sidetone level", [])
        self.sidetone_gain_row.connect("notify::selected", self._on_sidetone)
        calls.add(self.sidetone_row)
        calls.add(self.sidetone_gain_row)
        page.add(calls)

        dev = Adw.PreferencesGroup(title="Device")
        self.multipoint_row = Adw.SwitchRow(title="Multipoint", subtitle="Stay connected to two devices")
        self.multipoint_row.connect("notify::active", self._on_multipoint)
        self.prompt_row = _combo("Voice guidance", list(PROMPT_LABELS.values()))
        self.prompt_row.connect("notify::selected", self._on_prompt)
        self.assistant_row = _combo("Voice assistant", list(ASSISTANT_LABELS.values()))
        self.assistant_row.connect("notify::selected", self._on_assistant)
        self.name_row = Adw.EntryRow(title="Bluetooth name", show_apply_button=True)
        self.name_row.connect("apply", self._on_name)
        pair = Adw.ButtonRow(title="Enter pairing mode", start_icon_name="bluetooth-symbolic")
        pair.connect("activated", self._on_pairing)
        for w in (self.multipoint_row, self.prompt_row, self.assistant_row, self.name_row, pair):
            dev.add(w)
        page.add(dev)
        return page

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
            except (CommandError, TimeoutError, OSError, ValueError) as e:
                log.warning("%s failed: %s", what or fn, e)
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
            self._show_disconnected("Connection lost")
        elif what:
            self.toast(f"Could not {what}: {err}")
        return False

    # ---- connection ----------------------------------------------------

    def reconnect(self) -> None:
        self._teardown()
        try:
            device = bluez.find_device()
        except (bluez.BluezError, GLib.Error) as e:
            self._show_disconnected(str(e))
            return
        self.stack.set_visible_child_name("connecting")
        self.title.set_title(device.name.removeprefix("LE_"))
        self.title.set_subtitle(device.address)
        bluez.connect_async(device, self._on_connected)

    def _on_connected(self, conn: bluez.Connection | None, err: str | None) -> None:
        if conn is None:
            self._show_disconnected(err or "Connection failed")
            return
        self._conn = conn
        client = RtkClient(conn.sock)
        client.on_at_notification.append(lambda m: GLib.idle_add(self._on_notification, m))
        client.on_disconnect.append(lambda: GLib.idle_add(self._on_device_lost, client))
        self._hp = Headphones(client)
        model = self._hp.model
        # Replacing a model resets the selection; don't let that be written to the device.
        self._updating = True
        try:
            self.eq_row.set_model(Gtk.StringList.new([p.name for p in model.eq_presets]))
            self.sidetone_gain_row.set_model(Gtk.StringList.new(list(model.sidetone_gains)))
        finally:
            self._updating = False
        self.stack.set_visible_child_name("settings")
        self.load_all()
        self._battery_timer = GLib.timeout_add_seconds(BATTERY_POLL_S, self._poll_battery)

    def _on_device_lost(self, client: RtkClient) -> bool:
        if self._hp and self._hp.client is client:
            self._show_disconnected("Headphones disconnected")
        return False

    def _show_disconnected(self, reason: str) -> None:
        self._teardown()
        self.status_page.set_title("Headphones not connected")
        self.status_page.set_description(reason)
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

    def _on_close(self, *_args) -> bool:
        self._teardown()
        self._io.shutdown(wait=False, cancel_futures=True)
        return False

    # ---- reading state -------------------------------------------------

    def load_all(self) -> None:
        model = self._hp.model
        self.run(lambda hp: hp.name(), self._set_name)
        self.run(lambda hp: hp.battery(), self._set_battery)
        self.run(lambda hp: hp.streaming_codec(), self._set_streaming)
        self.run(lambda hp: hp.eq_bank(), self._set_eq)
        self.run(lambda hp: hp.codec(), self._set_codec)
        self.run(lambda hp: hp.low_latency(), self.latency_row.set_active)
        self.run(lambda hp: hp.sidetone(), self._set_sidetone)
        self.run(lambda hp: hp.multipoint(), self.multipoint_row.set_active)
        self.run(lambda hp: hp.voice_prompt(), lambda v: self._select(self.prompt_row, list(PROMPT_LABELS), v))
        self.run(
            lambda hp: hp.voice_assistant(),
            lambda v: self._select(self.assistant_row, list(ASSISTANT_LABELS), v),
        )
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
        self.streaming_label.set_label(getattr(codec, "name", str(codec)).replace("_", " "))

    def _set_codec(self, codec: Codec) -> None:
        self._select(self.codec_row, list(CODEC_LABELS), codec)
        self.latency_row.set_sensitive(codec not in self._hp.model.low_latency_unsupported)

    def _set_eq(self, bank: int) -> None:
        banks = [p.bank for p in self._hp.model.eq_presets]
        if bank in banks:
            self.eq_row.set_selected(banks.index(bank))

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
        finally:
            self._updating = False
        return False

    # ---- user changes --------------------------------------------------

    def _on_eq(self, row, _pspec) -> None:
        if self._updating or not self._hp:
            return
        preset = self._hp.model.eq_presets[row.get_selected()]
        self.run(lambda hp: hp.set_eq_bank(preset.bank), what="change the equalizer")

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

    def _on_pairing(self, _row) -> None:
        self.run(lambda hp: hp.enter_pairing_mode(), lambda _: self.toast("Pairing mode on"),
                 what="enter pairing mode")


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
