import threading

import urwid

from railmux.winlocal.virtual_screen import VirtualScreen


def test_virtual_screen_captures_urwid_canvas_and_decodes_vt_input():
    screen = VirtualScreen(30, 6)
    screen.start()
    try:
        canvas = urwid.Filler(urwid.Text("Windows controller")).render((30, 6), True)
        screen.draw_screen((30, 6), canvas)
        screen.inject(b"a\033[A")
        keys, raw = screen.get_input(True)

        assert "a" in keys
        assert "up" in keys
        assert raw
        assert any(b"Windows controller" in row for row in screen.pane.rows)
    finally:
        screen.close()


def test_virtual_screen_reports_resize_event():
    screen = VirtualScreen(30, 6)
    screen.start()
    try:
        screen.resize(50, 12)
        keys, _raw = screen.get_input(True)
        assert "window resize" in keys
        assert screen.get_cols_rows() == (50, 12)
    finally:
        screen.close()


def test_virtual_screen_runs_urwid_event_loop_off_main_thread():
    errors = []
    finished = threading.Event()

    def run():
        screen = VirtualScreen(80, 24)
        loop = urwid.MainLoop(urwid.Filler(urwid.Text("ready")), screen=screen)

        def stop(_loop, _data):
            raise urwid.ExitMainLoop()

        loop.set_alarm_in(0.02, stop)
        try:
            loop.run()
        except BaseException as exc:
            errors.append(exc)
        finally:
            screen.close()
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=2)

    assert finished.is_set()
    assert errors == []
