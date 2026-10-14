from typing import Any

import pytest

from portal.cli import app
from tests.test_boxes import all_instances, aws, runner  # noqa: F401  (aws is a fixture)
from tests.test_home import UP, home_volumes


def test_purge_needs_the_box_name_typed_even_with_yes(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    # A single keystroke ("y", or just Enter) must not delete anything.
    for answer in ("y\n", "\n", "h2\n"):
        result = runner.invoke(app, ["down", "--name", "h1", "--purge", "--yes"], input=answer)
        assert "nothing deleted" in result.output or result.exit_code != 0
        assert len(home_volumes(aws)) == 1
        assert all_instances(aws)[0]["State"]["Name"] == "running"


def test_purge_with_the_right_name_deletes_box_and_volume(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    result = runner.invoke(app, ["down", "--name", "h1", "--purge"], input="h1\n")
    assert result.exit_code == 0, result.output
    assert home_volumes(aws) == []
    assert all_instances(aws)[0]["State"]["Name"] in ("shutting-down", "terminated")


def test_purge_works_when_only_the_volume_is_left(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    assert runner.invoke(app, ["down", "--name", "h1", "--yes"]).exit_code == 0
    assert len(home_volumes(aws)) == 1
    result = runner.invoke(app, ["down", "--name", "h1", "--purge"], input="h1\n")
    assert result.exit_code == 0, result.output
    assert home_volumes(aws) == []


def test_keep_home_is_the_default(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    result = runner.invoke(app, ["down", "--name", "h1", "--keep-home", "--yes"])
    assert "kept" in result.output
    assert len(home_volumes(aws)) == 1


def test_volumes_lists_and_flags_orphans(
    aws: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    (tmp_path / ".portal.yaml").write_text("gp3_usd_per_gb_month: 0.1\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, UP).exit_code == 0
    assert runner.invoke(app, [*UP[:-1], "h2"]).exit_code == 0
    assert runner.invoke(app, ["down", "--name", "h1", "--yes"]).exit_code == 0
    result = runner.invoke(app, ["volumes"], env={"COLUMNS": "160"})
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.strip()}
    assert "orphaned" in lines["h1"]
    assert "orphaned" not in lines["h2"]
    assert "2.00" in lines["h1"]  # 20 GB x $0.10


def test_volumes_without_price_says_how_to_get_one(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    result = runner.invoke(app, ["volumes"], env={"COLUMNS": "160"})
    assert "gp3_usd_per_gb_month" in result.output


def test_up_warns_about_other_orphaned_volumes(aws: Any) -> None:  # noqa: F811
    assert runner.invoke(app, UP).exit_code == 0
    assert runner.invoke(app, ["down", "--name", "h1", "--yes"]).exit_code == 0
    wide = {"COLUMNS": "200"}
    other = runner.invoke(app, [*UP[:-1], "h2"], env=wide)
    assert other.exit_code == 0, other.output
    assert "'h1', which is not running" in other.output
    # Reusing your own volume is not an orphan warning.
    assert runner.invoke(app, ["down", "--name", "h2", "--yes"]).exit_code == 0
    again = runner.invoke(app, UP, env=wide)
    assert "'h1', which is not running" not in again.output
