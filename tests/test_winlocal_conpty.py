from railmux.winlocal.conpty import PyWinPtyProcess, _cmd_shim_command_line


class _FakePty:
    pid = 42

    def __init__(self):
        self.writes = []
        self.size = None
        self.alive = True

    def read(self, size):
        assert size == 9
        return "hello 世界"

    def write(self, text):
        self.writes.append(text)
        return len(text)

    def setwinsize(self, rows, columns):
        self.size = (rows, columns)

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False
        return True


def test_pywinpty_facade_is_utf8_and_uses_rows_columns_order():
    raw = _FakePty()
    process = PyWinPtyProcess(raw)

    assert process.pid == 42
    assert process.read(9) == "hello 世界".encode()
    process.write("输入".encode())
    process.resize(120, 30)

    assert raw.writes == ["输入"]
    assert raw.size == (30, 120)
    assert process.terminate(force=True)
    assert not process.is_alive()


def test_cmd_shim_uses_one_explicit_command_line():
    command = _cmd_shim_command_line(
        (
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            '"C:\\Program Files\\nodejs\\codex.cmd" resume "session id"',
        )
    )

    assert command == (
        ' /d /s /c ""C:\\Program Files\\nodejs\\codex.cmd" '
        'resume "session id""'
    )
