"""BlueZ integration: find paired headphones and open the vendor RFCOMM channel.

Realtek based models speak the vendor protocol either on a dedicated vendor
RFCOMM service or, like the ATH-M50xBT2, on the standard Serial Port service.
The RFCOMM channel number is only known through SDP, so
instead of doing SDP ourselves we register an external client profile for the
vendor UUID and ask BlueZ to connect it. BlueZ performs the SDP lookup, opens
the RFCOMM socket and hands us its file descriptor via Profile1.NewConnection.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Gio, GLib

from .protocol import RTK_SPP_UUID, SPP_UUID

BLUEZ = "org.bluez"
PROFILE_PATH = "/io/github/atconnect/profile"

_PROFILE_XML = """
<node>
  <interface name="org.bluez.Profile1">
    <method name="Release"/>
    <method name="NewConnection">
      <arg type="o" name="device" direction="in"/>
      <arg type="h" name="fd" direction="in"/>
      <arg type="a{sv}" name="properties" direction="in"/>
    </method>
    <method name="RequestDisconnection">
      <arg type="o" name="device" direction="in"/>
    </method>
  </interface>
</node>
"""


class BluezError(Exception):
    pass


@dataclass(frozen=True)
class Device:
    path: str
    address: str
    name: str
    connected: bool
    has_vendor_service: bool

    @property
    def profile_uuid(self) -> str:
        """Realtek vendor service if advertised, otherwise the plain Serial Port (ATH-M50xBT2)."""
        return RTK_SPP_UUID if self.has_vendor_service else SPP_UUID

    def __str__(self) -> str:
        state = "connected" if self.connected else "disconnected"
        return f"{self.address}  {self.name}  ({state})"


def _bus() -> Gio.DBusConnection:
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def list_devices(only_supported: bool = True) -> list[Device]:
    """Paired BlueZ devices that look like Audio-Technica headphones."""
    bus = _bus()
    reply = bus.call_sync(
        BLUEZ, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
        None, GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, -1, None,
    )
    (objects,) = reply.unpack()
    devices = []
    for path, ifaces in objects.items():
        props = ifaces.get("org.bluez.Device1")
        if not props or not props.get("Paired"):
            continue
        uuids = [u.lower() for u in props.get("UUIDs", [])]
        name = props.get("Alias") or props.get("Name") or ""
        dev = Device(
            path=path,
            address=props.get("Address", ""),
            name=name,
            connected=bool(props.get("Connected")),
            has_vendor_service=RTK_SPP_UUID in uuids,
        )
        if only_supported and not (dev.has_vendor_service or "ATH-" in name.upper()):
            continue
        devices.append(dev)
    devices.sort(key=lambda d: (not d.connected, d.name))
    return devices


def find_device(address: str | None = None) -> Device:
    devices = list_devices(only_supported=address is None)
    if address:
        for d in devices:
            if d.address.lower() == address.lower():
                return d
        raise BluezError(f"device {address} is not paired")
    connected = [d for d in devices if d.connected]
    if not connected:
        if devices:
            raise BluezError(
                "headphones are paired but not connected: " + ", ".join(str(d) for d in devices)
            )
        raise BluezError("no paired Audio-Technica headphones found")
    return connected[0]


class _ProfileConnector:
    def __init__(self, bus: Gio.DBusConnection) -> None:
        self.bus = bus
        self.fd: int | None = None
        self.error: str | None = None
        self.on_disconnect_request = None
        self.on_new_connection: Callable[[], None] | None = None
        info = Gio.DBusNodeInfo.new_for_xml(_PROFILE_XML).interfaces[0]
        self._reg_id = bus.register_object(PROFILE_PATH, info, self._on_call, None, None)
        self._registered = False

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        if method == "NewConnection":
            _device, fd_index, _props = params.unpack()
            fd_list = invocation.get_message().get_unix_fd_list()
            self.fd = fd_list.get(fd_index)  # returns a dup()ed descriptor
            if self.on_new_connection:
                GLib.idle_add(self.on_new_connection)
        elif method == "RequestDisconnection" and self.on_disconnect_request:
            self.on_disconnect_request()
        invocation.return_value(None)

    def register(self, uuid: str) -> None:
        opts = {
            "Name": GLib.Variant("s", "atconnect"),
            "Role": GLib.Variant("s", "client"),
            "AutoConnect": GLib.Variant("b", False),
        }
        try:
            self.bus.call_sync(
                BLUEZ, "/org/bluez", "org.bluez.ProfileManager1", "RegisterProfile",
                GLib.Variant("(osa{sv})", (PROFILE_PATH, uuid, opts)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as e:
            if "AlreadyExists" not in e.message:
                raise BluezError(f"RegisterProfile failed: {e.message}") from e
        self._registered = True

    def unregister(self) -> None:
        if self._registered:
            try:
                self.bus.call_sync(
                    BLUEZ, "/org/bluez", "org.bluez.ProfileManager1", "UnregisterProfile",
                    GLib.Variant("(o)", (PROFILE_PATH,)), None, Gio.DBusCallFlags.NONE, -1, None,
                )
            except GLib.Error:
                pass
            self._registered = False
        if self._reg_id:
            self.bus.unregister_object(self._reg_id)
            self._reg_id = 0


class Connection:
    """An open vendor RFCOMM channel. Keep it alive for as long as the socket is used."""

    def __init__(self, sock: socket.socket, device: Device, connector: _ProfileConnector | None):
        self.sock = sock
        self.device = device
        self._connector = connector

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        if self._connector:
            try:
                self._connector.bus.call_sync(
                    BLUEZ, self.device.path, "org.bluez.Device1", "DisconnectProfile",
                    GLib.Variant("(s)", (self.device.profile_uuid,)), None, Gio.DBusCallFlags.NONE, -1, None,
                )
            except GLib.Error:
                pass
            self._connector.unregister()
            self._connector = None


def connect_async(
    device: Device,
    on_done: Callable[[Connection | None, str | None], None],
    timeout: float = 15.0,
) -> None:
    """Open the vendor channel without blocking; on_done(conn, error) runs in the GLib main loop."""
    bus = _bus()
    connector = _ProfileConnector(bus)
    state = {"finished": False, "timer": 0}

    def finish(error: str | None) -> None:
        if state["finished"]:
            return
        state["finished"] = True
        if state["timer"]:
            GLib.source_remove(state["timer"])
        if error is None and connector.fd is not None:
            sock = socket.socket(fileno=connector.fd)
            sock.setblocking(True)
            on_done(Connection(sock, device, connector), None)
            return
        if connector.fd is not None:
            os.close(connector.fd)
        connector.unregister()
        on_done(None, error or "connection failed")

    def on_timeout() -> bool:
        state["timer"] = 0
        finish("timed out waiting for the vendor RFCOMM channel")
        return False

    def on_reply(source, result) -> None:
        try:
            source.call_finish(result)
        except GLib.Error as e:
            finish(f"ConnectProfile failed: {e.message}")
            return
        if connector.fd is not None:
            finish(None)

    connector.on_new_connection = lambda: finish(None)
    try:
        connector.register(device.profile_uuid)
    except BluezError as e:
        connector.unregister()
        GLib.idle_add(lambda: on_done(None, str(e)))
        return
    state["timer"] = GLib.timeout_add(int(timeout * 1000), on_timeout)
    bus.call(
        BLUEZ, device.path, "org.bluez.Device1", "ConnectProfile",
        GLib.Variant("(s)", (device.profile_uuid,)), None, Gio.DBusCallFlags.NONE,
        int(timeout * 1000), None, on_reply,
    )


def connect(device: Device, timeout: float = 15.0, channel: int | None = None) -> Connection:
    """Open the vendor channel, iterating the default GLib context until it is ready."""
    if channel is not None:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(timeout)
        sock.connect((device.address, channel))
        sock.settimeout(None)
        return Connection(sock, device, None)

    result: dict = {}
    connect_async(device, lambda conn, err: result.update(conn=conn, err=err), timeout)
    ctx = GLib.MainContext.default()
    while not result:
        ctx.iteration(True)
    if result["conn"] is None:
        raise BluezError(result["err"])
    return result["conn"]
