"""In-process VT Urwid screen used by the Windows daemon controller."""
from __future__ import annotations

import functools
import selectors
import socket
from collections.abc import Callable

from urwid.display import escape
from urwid.display._raw_display_base import Screen as RawBaseScreen

from railmux.winlocal.compositor import TerminalPane


class _PaneWriter:
    def __init__(self, pane: TerminalPane) -> None:
        self.pane = pane

    def write(self, data: str) -> None:
        self.pane.feed(data.encode("utf-8", errors="replace"))

    def flush(self) -> None:
        pass


class VirtualScreen(RawBaseScreen):
    """Urwid screen whose bytes and input remain inside the daemon."""

    def __init__(self, columns: int, rows: int) -> None:
        self.pane = TerminalPane(columns, rows)
        self._columns = columns
        self._rows = rows
        self._input_reader, self._input_writer = socket.socketpair()
        self._input_reader.setblocking(False)
        super().__init__(self._input_reader, _PaneWriter(self.pane))

    def close(self) -> None:
        self.stop()
        self._input_reader.close()
        self._input_writer.close()

    def inject(self, data: bytes) -> None:
        self._input_writer.sendall(data)

    def resize(self, columns: int, rows: int) -> None:
        self._columns = columns
        self._rows = rows
        self.pane.resize(columns, rows)
        self._sigwinch_handler()

    def get_cols_rows(self) -> tuple[int, int]:
        return self._columns, self._rows

    def _start(self, alternate_buffer: bool = True, *args, **kwargs) -> None:
        self._alternate_buffer = alternate_buffer
        if alternate_buffer:
            self.write(escape.SWITCH_TO_ALTERNATE_BUFFER)
        self.write(escape.HIDE_CURSOR)
        self.flush()

    def _stop(self) -> None:
        self._stop_mouse_restore_buffer()

    def _get_keyboard_codes(self) -> list[int]:
        result = bytearray()
        while True:
            try:
                chunk = self._input_reader.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            result.extend(chunk)
        return list(result)

    def _read_raw_input(self, timeout: float | None) -> bytearray:
        if self._input_reader.fileno() not in self._wait_for_input_ready(timeout):
            return bytearray()
        return bytearray(self._get_keyboard_codes())

    def _wait_for_input_ready(self, timeout: float | None):
        with selectors.DefaultSelector() as selector:
            for descriptor in self.get_input_descriptors():
                selector.register(descriptor, selectors.EVENT_READ)
            return [key.fd for key, _mask in selector.select(timeout)]

    def hook_event_loop(self, event_loop, callback: Callable) -> None:
        @functools.wraps(callback)
        def wrapper():
            return self.parse_input(
                event_loop, callback, self.get_available_raw_input()
            )

        self._current_event_loop_handles = [
            event_loop.watch_file(
                descriptor if isinstance(descriptor, int) else descriptor.fileno(),
                wrapper,
            )
            for descriptor in self.get_input_descriptors()
        ]

    def unhook_event_loop(self, event_loop) -> None:
        for handle in self._current_event_loop_handles:
            event_loop.remove_watch_file(handle)
        self._current_event_loop_handles = ()
