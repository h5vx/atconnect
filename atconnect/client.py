"""Request/response engine on top of a connected vendor RFCOMM socket."""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .protocol import (
    NO_ACK_OPCODES,
    Ack,
    AtMessage,
    Decoder,
    Op,
    Packet,
    ProtocolError,
    ack_packet,
    at_request,
)

log = logging.getLogger(__name__)

ACK_TIMEOUT = 1.0
ACK_RETRIES = 2
RESPONSE_TIMEOUT = 3.0


class CommandError(Exception):
    def __init__(self, what: str, status: int):
        try:
            name = Ack(status).name
        except ValueError:
            name = str(status)
        super().__init__(f"{what}: {name}")
        self.status = status


class Disconnected(Exception):
    pass


@dataclass
class _Waiter:
    match: Callable[[Packet], bool]
    event: threading.Event = field(default_factory=threading.Event)
    packet: Packet | None = None


class RtkClient:
    """Talks the Realtek transport protocol. Thread-safe; callbacks run on the reader thread."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._seq = 0
        self._last_rx_seq: int | None = None
        self._tx_lock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._waiters_lock = threading.Lock()
        self._ack_waiter: _Waiter | None = None
        self._closed = threading.Event()
        self.on_packet: list[Callable[[Packet], None]] = []
        self.on_at_notification: list[Callable[[AtMessage], None]] = []
        self.on_disconnect: list[Callable[[], None]] = []
        self._reader = threading.Thread(target=self._read_loop, name="rtk-reader", daemon=True)
        self._reader.start()

    # ---- low level -------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq = 1 if self._seq >= 255 else self._seq + 1
        return self._seq

    def _write(self, opcode: int, params: bytes) -> Packet:
        with self._tx_lock:
            pkt = Packet(self._next_seq(), opcode, params)
            log.debug("<< %s", pkt)
            try:
                self._sock.sendall(pkt.encode())
            except OSError as e:
                raise Disconnected(str(e)) from e
            return pkt

    def _write_ack(self, opcode: int) -> None:
        with self._tx_lock:
            pkt = ack_packet(self._next_seq(), opcode)
            log.debug("<< %s", pkt)
            try:
                self._sock.sendall(pkt.encode())
            except OSError:
                pass

    def _read_loop(self) -> None:
        decoder = Decoder()
        try:
            while not self._closed.is_set():
                data = self._sock.recv(4096)
                if not data:
                    break
                for pkt in decoder.feed(data):
                    self._handle(pkt)
        except OSError:
            pass
        finally:
            self._closed.set()
            with self._waiters_lock:
                for w in self._waiters:
                    w.event.set()
                if self._ack_waiter:
                    self._ack_waiter.event.set()
            for cb in self.on_disconnect:
                cb()

    def _handle(self, pkt: Packet) -> None:
        log.debug(">> %s", pkt)
        duplicate = pkt.seq == self._last_rx_seq
        self._last_rx_seq = pkt.seq
        if pkt.is_ack:
            if duplicate:
                return
            with self._waiters_lock:
                w = self._ack_waiter
                if w and w.match(pkt):
                    w.packet = pkt
                    w.event.set()
            return
        if pkt.opcode not in NO_ACK_OPCODES:
            self._write_ack(pkt.opcode)
        if duplicate:
            return
        with self._waiters_lock:
            for w in self._waiters:
                if w.packet is None and w.match(pkt):
                    w.packet = pkt
                    w.event.set()
                    break
        if pkt.opcode == Op.AT_RECEIVE:
            try:
                msg = AtMessage.parse(pkt.params)
            except ProtocolError as e:
                log.warning("%s", e)
                return
            if msg.is_notification:
                for cb in self.on_at_notification:
                    cb(msg)
        for cb in self.on_packet:
            cb(pkt)

    # ---- public API ------------------------------------------------------

    @property
    def connected(self) -> bool:
        return not self._closed.is_set()

    def close(self) -> None:
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def request(
        self,
        opcode: int,
        params: bytes = b"",
        expect: Callable[[Packet], bool] | None = None,
        timeout: float = RESPONSE_TIMEOUT,
    ) -> Packet | None:
        """Send a command, wait for its ACK and optionally for a matching event."""
        if self._closed.is_set():
            raise Disconnected("not connected")
        with self._cmd_lock:
            waiter = _Waiter(expect) if expect else None
            if waiter:
                with self._waiters_lock:
                    self._waiters.append(waiter)
            try:
                self._send_with_ack(opcode, params)
                if not waiter:
                    return None
                if not waiter.event.wait(timeout) or waiter.packet is None:
                    if self._closed.is_set():
                        raise Disconnected("connection closed")
                    raise TimeoutError(f"no response to 0x{opcode:04x}")
                return waiter.packet
            finally:
                if waiter:
                    with self._waiters_lock:
                        self._waiters.remove(waiter)

    def _send_with_ack(self, opcode: int, params: bytes) -> None:
        ack = _Waiter(lambda p: len(p.params) >= 3 and p.ack_info()[0] == opcode)
        with self._waiters_lock:
            self._ack_waiter = ack
        try:
            for _ in range(ACK_RETRIES + 1):
                self._write(opcode, params)
                if ack.event.wait(ACK_TIMEOUT) and ack.packet is not None:
                    _, status = ack.packet.ack_info()
                    if status != Ack.COMPLETE:
                        raise CommandError(f"command 0x{opcode:04x}", status)
                    return
                if self._closed.is_set():
                    raise Disconnected("connection closed")
            raise TimeoutError(f"no ACK for 0x{opcode:04x}")
        finally:
            with self._waiters_lock:
                self._ack_waiter = None

    def at(self, sub_id: int, payload: bytes = b"", timeout: float = RESPONSE_TIMEOUT) -> bytes:
        """Run an Audio-Technica vendor command and return its response payload."""

        def match(p: Packet) -> bool:
            if p.opcode != Op.AT_RECEIVE or len(p.params) < 2:
                return False
            msg = AtMessage.parse(p.params)
            return not msg.is_notification and msg.sub_id == sub_id

        pkt = self.request(Op.AT_SEND, at_request(sub_id, payload), match, timeout)
        assert pkt is not None
        msg = AtMessage.parse(pkt.params)
        if msg.status != Ack.COMPLETE:
            raise CommandError(f"AT command {sub_id}", msg.status or Ack.PROCESS_FAIL)
        return msg.payload
