from __future__ import annotations

import io
from email.message import Message
from pathlib import Path

import pytest

from railmux.windows_pacman import (
    PACMAN_MIRROR_SOURCES,
    PacmanMirrorError,
    PacmanMirrorProbe,
    deactivate_pacman_hosts,
    optimize_pacman_mirror,
    probe_pacman_mirror,
    validate_transaction_package_mirrors,
    write_msys_only_pacman_config,
)


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 206,
        final_url: str = "https://mirror.invalid/msys.db",
        last_modified: str | None = None,
    ) -> None:
        super().__init__(payload)
        self.status = status
        self._final_url = final_url
        self.headers = Message()
        if last_modified is not None:
            self.headers["Last-Modified"] = last_modified

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def geturl(self):
        return self._final_url


def write_mirrorlist(root: Path, servers=None) -> Path:
    mirrorlist = root / "etc" / "pacman.d" / "mirrorlist.msys"
    mirrorlist.parent.mkdir(parents=True)
    selected_servers = (
        [server for _label, server in PACMAN_MIRROR_SOURCES]
        if servers is None
        else servers
    )
    mirrorlist.write_text(
        "# official list\n## Primary\n"
        + "\n".join(
            f"Server = {server}" for server in selected_servers[:2]
        )
        + "\n## Tier 1\n"
        + "\n".join(
            f"Server = {server}" for server in selected_servers[2:]
        )
        + "\n# retained footer\n",
        encoding="utf-8",
    )
    return mirrorlist


def mirror_probe(label, server, *, rate, modified_at=None):
    return PacmanMirrorProbe(label, server, int(rate), 1.0, modified_at)


def test_optimizer_promotes_a_materially_faster_approved_official_mirror(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    rates = {
        "MSYS2 geo mirror": 100,
        "MSYS2 repository": 80,
        "TUNA mirror": 500,
        "USTC mirror": 400,
        "NJU mirror": 300,
    }

    decision = optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(
            label, server, rate=rates[label]
        ),
    )

    assert decision.selected is not None
    assert decision.selected.label == "TUNA mirror"
    assert decision.changed
    lines = mirrorlist.read_text(encoding="utf-8").splitlines()
    assert lines[0:2] == ["# official list", "## Primary"]
    assert lines[2] == f"Server = {decision.selected.server}"
    assert "## Tier 1" in lines
    assert lines[-1] == "# retained footer"
    assert sum(line.startswith("Server = ") for line in lines) == len(
        PACMAN_MIRROR_SOURCES
    )


def test_optimizer_keeps_official_primary_for_an_immaterial_difference(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    original = mirrorlist.read_bytes()
    primary = PACMAN_MIRROR_SOURCES[0][1]

    decision = optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(
            label,
            server,
            rate=110 if label == "TUNA mirror" else 100,
        ),
    )

    assert decision.selected is not None
    assert decision.selected.server == primary
    assert decision.changed
    assert mirrorlist.read_bytes() != original
    assert len(decision.active) == len(PACMAN_MIRROR_SOURCES)
    assert mirrorlist.read_text(encoding="utf-8").splitlines()[2] == (
        f"Server = {primary}"
    )


def test_optimizer_rejects_a_fast_but_stale_database(tmp_path):
    write_mirrorlist(tmp_path)
    primary = PACMAN_MIRROR_SOURCES[0][1]

    decision = optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(
            label,
            server,
            rate=1000 if label == "TUNA mirror" else 100,
            modified_at=100 if label == "TUNA mirror" else 100 + 24 * 60 * 60,
        ),
    )

    assert decision.selected is not None
    assert decision.selected.server == primary
    assert decision.changed
    assert decision.active[-1].label == "TUNA mirror"


def test_undated_primary_still_requires_a_material_speed_gain(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    original = mirrorlist.read_bytes()

    decision = optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(
            label,
            server,
            rate=110 if label == "TUNA mirror" else 100,
            modified_at=None if label == "MSYS2 geo mirror" else 100,
        ),
    )

    assert decision.selected is not None
    assert decision.selected.server == PACMAN_MIRROR_SOURCES[0][1]
    assert decision.changed
    assert mirrorlist.read_bytes() != original


def test_optimizer_never_probes_or_removes_an_unapproved_official_entry(tmp_path):
    unapproved = "https://example.invalid/msys/$arch/"
    tuna = PACMAN_MIRROR_SOURCES[2][1]
    mirrorlist = write_mirrorlist(tmp_path, [unapproved, tuna])
    attempted = []

    def probe(label, server):
        attempted.append(server)
        return mirror_probe(label, server, rate=100)

    decision = optimize_pacman_mirror(tmp_path, probe=probe)

    assert attempted == [tuna]
    assert decision.selected is not None and decision.selected.server == tuna
    rendered = mirrorlist.read_text(encoding="utf-8")
    assert rendered.index(tuna) < rendered.index(unapproved)
    assert unapproved in rendered


def test_optimizer_retains_the_original_list_when_every_probe_fails(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    original = mirrorlist.read_bytes()

    def fail(_label, _server):
        raise PacmanMirrorError("offline")

    decision = optimize_pacman_mirror(tmp_path, probe=fail)

    assert decision.selected is None
    assert not decision.changed
    assert len(decision.failures) == len(PACMAN_MIRROR_SOURCES)
    assert mirrorlist.read_bytes() == original


def test_optimizer_refuses_a_symlinked_private_mirrorlist(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("Server = https://example.invalid/\n", encoding="utf-8")
    mirrorlist = tmp_path / "runtime" / "etc" / "pacman.d" / "mirrorlist.msys"
    mirrorlist.parent.mkdir(parents=True)
    mirrorlist.symlink_to(outside)

    with pytest.raises(PacmanMirrorError, match="safe regular file"):
        optimize_pacman_mirror(tmp_path / "runtime")


def test_network_probe_is_bounded_and_rejects_an_https_downgrade():
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse(
            b"\x28\xb5\x2f\xfd" + b"x" * (300 * 1024),
            final_url="http://mirror.invalid/msys.db",
        )

    with pytest.raises(PacmanMirrorError, match="outside HTTPS"):
        probe_pacman_mirror(
            "test",
            "https://mirror.invalid/msys/$arch/",
            opener=opener,
        )

    assert requests[0][0].get_header("Range") == "bytes=0-262143"
    assert requests[0][1] == 8.0


def test_optimizer_limits_active_pool_but_reprobes_inactive_candidates(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    nju = PACMAN_MIRROR_SOURCES[-1][1]

    def first_probe(label, server):
        if server == nju:
            raise PacmanMirrorError("offline")
        return mirror_probe(label, server, rate=100)

    first = optimize_pacman_mirror(tmp_path, probe=first_probe)
    rendered = mirrorlist.read_text(encoding="utf-8")
    assert len(first.active) == len(PACMAN_MIRROR_SOURCES) - 1
    assert f"# Railmux inactive: Server = {nju}" in rendered

    second = optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(label, server, rate=100),
    )
    assert len(second.active) == len(PACMAN_MIRROR_SOURCES)
    assert f"Server = {nju}" in mirrorlist.read_text(encoding="utf-8")


def test_hard_failing_host_is_removed_from_active_pool(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(label, server, rate=100),
    )
    tuna = PACMAN_MIRROR_SOURCES[2][1]

    assert deactivate_pacman_hosts(tmp_path, ["mirrors.tuna.tsinghua.edu.cn"])

    rendered = mirrorlist.read_text(encoding="utf-8")
    assert f"# Railmux inactive: Server = {tuna}" in rendered
    assert f"\nServer = {tuna}\n" not in rendered


def test_private_pacman_config_keeps_options_and_only_msys_repo(tmp_path):
    config = tmp_path / "etc" / "pacman.conf"
    config.parent.mkdir(parents=True)
    config.write_text(
        "# pacman\n[options]\nParallelDownloads = 5\n"
        "[mingw64]\nInclude = /etc/pacman.d/mirrorlist.mingw\n"
        "[ucrt64]\nInclude = /etc/pacman.d/mirrorlist.mingw\n"
        "[msys]\nInclude = /etc/pacman.d/mirrorlist.msys\n",
        encoding="utf-8",
    )

    destination = write_msys_only_pacman_config(tmp_path)

    rendered = destination.read_text(encoding="utf-8")
    assert "ParallelDownloads = 5" in rendered
    assert "[msys]" in rendered
    assert "mirrorlist.msys" in rendered
    assert "[mingw64]" not in rendered
    assert "[ucrt64]" not in rendered


def test_transaction_package_probe_removes_database_only_mirrors(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(label, server, rate=100),
    )
    tuna = PACMAN_MIRROR_SOURCES[2][1]
    checked = []

    def probe(url):
        checked.append(url)
        if "tuna.tsinghua" in url:
            raise PacmanMirrorError("package returned HTTP 403")

    decision = validate_transaction_package_mirrors(
        tmp_path,
        "https://repo.msys2.org/msys/x86_64/python-3.12.pkg.tar.zst",
        probe=probe,
    )

    assert len(checked) == len(PACMAN_MIRROR_SOURCES)
    assert decision.package_names == ("python-3.12.pkg.tar.zst",)
    assert len(decision.active_servers) == len(PACMAN_MIRROR_SOURCES) - 1
    assert decision.changed
    assert decision.failures == ((
        "TUNA mirror",
        "python-3.12.pkg.tar.zst: package returned HTTP 403",
    ),)
    rendered = mirrorlist.read_text(encoding="utf-8")
    assert f"# Railmux inactive: Server = {tuna}" in rendered


def test_transaction_package_probe_keeps_pool_when_every_probe_fails(tmp_path):
    mirrorlist = write_mirrorlist(tmp_path)
    optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(label, server, rate=100),
    )
    before = mirrorlist.read_text(encoding="utf-8")

    decision = validate_transaction_package_mirrors(
        tmp_path,
        "https://repo.msys2.org/msys/x86_64/tmux.pkg.tar.zst",
        probe=lambda _url: (_ for _ in ()).throw(
            PacmanMirrorError("temporary failure")
        ),
    )

    assert not decision.changed
    assert len(decision.active_servers) == len(PACMAN_MIRROR_SOURCES)
    assert mirrorlist.read_text(encoding="utf-8") == before


def test_transaction_package_probe_rejects_non_package_url(tmp_path):
    write_mirrorlist(tmp_path)

    with pytest.raises(PacmanMirrorError, match="invalid package probe URL"):
        validate_transaction_package_mirrors(
            tmp_path,
            "https://repo.msys2.org/msys/x86_64/msys.db",
        )


def test_transaction_probe_excludes_mirror_missing_a_later_package(tmp_path):
    write_mirrorlist(tmp_path)
    optimize_pacman_mirror(
        tmp_path,
        probe=lambda label, server: mirror_probe(label, server, rate=100),
    )
    tuna = PACMAN_MIRROR_SOURCES[2][1]

    def probe(url):
        if "tuna.tsinghua" in url and "python-" in url:
            raise PacmanMirrorError("package returned HTTP 403")

    decision = validate_transaction_package_mirrors(
        tmp_path,
        (
            "https://repo.msys2.org/msys/x86_64/tmux-3.7.pkg.tar.zst",
            "https://repo.msys2.org/msys/x86_64/python-3.12.pkg.tar.zst",
        ),
        probe=probe,
    )

    assert decision.package_names == (
        "tmux-3.7.pkg.tar.zst",
        "python-3.12.pkg.tar.zst",
    )
    assert tuna not in decision.active_servers
    assert decision.failures == ((
        "TUNA mirror",
        "python-3.12.pkg.tar.zst: package returned HTTP 403",
    ),)
