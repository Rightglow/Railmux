from railmux.ssh_compat import CompatibilityFacts, decide


def facts(**changes):
    values = {
        "local_version": "2.0",
        "local_protocol": 10,
        "remote_version": "2.0",
        "remote_protocol": 10,
        "remote_ready": True,
        "remote_tmux": True,
    }
    values.update(changes)
    return CompatibilityFacts(**values)


def test_newer_remote_consent_is_reentrant_and_decline_can_attach():
    candidate = facts(remote_version="3.0")

    prompt = decide(candidate)
    declined = decide(candidate, {"local_upgrade": False})
    accepted = decide(candidate, {"local_upgrade": True})

    assert prompt.prompt == "local_upgrade"
    assert declined.action == "attach"
    assert "continuing with local Railmux" in (declined.warning or "")
    assert accepted.action == "upgrade_local"
    assert accepted.install_version == "3.0"


def test_newer_not_ready_remote_repairs_its_newer_version_and_decline_is_fatal():
    candidate = facts(remote_version="3.0", remote_ready=False)
    declined_local = {"local_upgrade": False}

    prompt = decide(candidate, declined_local)
    install = decide(candidate, {**declined_local, "remote_install": True})
    refused = decide(candidate, {**declined_local, "remote_install": False})

    assert prompt.prompt == "remote_install"
    assert prompt.install_version == "3.0"
    assert install.action == "install_remote"
    assert install.install_version == "3.0"
    assert refused.action == "error"


def test_protocol_authority_precedes_tmux_and_package_version():
    older = facts(
        remote_version="1.0",
        remote_protocol=9,
        remote_tmux=False,
    )
    newer_protocol = facts(remote_version="not-semver", remote_protocol=11)

    decision = decide(older)
    assert decision.prompt == "remote_install"
    assert "older SSH protocol" in (decision.reason or "")
    assert decide(newer_protocol).action == "error"


def test_unparseable_compatible_version_warns_and_attaches():
    decision = decide(facts(remote_version="private-build"))

    assert decision.action == "attach"
    assert "differs from local" in (decision.warning or "")
