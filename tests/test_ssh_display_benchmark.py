from tools.ssh_display_benchmark import collect_benchmark, main


def test_ssh_display_wire_budget_is_deterministic_and_bounded(capsys):
    result = collect_benchmark()

    assert result.checks_passed
    assert result.one_row_patch_wire_bytes < result.keyframe_wire_bytes
    assert main(["--check", "--json"]) == 0
    assert '"checks_passed": true' in capsys.readouterr().out
