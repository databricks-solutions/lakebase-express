import os

import pytest

from scripts import azure_sql_connect as probe


def test_env_file_loader_handles_comments_quotes_and_password_punctuation(tmp_path, monkeypatch):
    env_file = tmp_path / "azure_sql.env"
    env_file.write_text(
        """# A comment containing = and an unmatched \" must be harmless.
LBX_SRC_HOST=server.database.windows.net
LBX_SRC_DATABASE='migration db'
export LBX_SRC_USER=sqladmin
LBX_SRC_PASSWORD=p@ss#word=still-the-password
"""
    )
    monkeypatch.setenv("LBX_SRC_HOST", "stale-host")

    loaded = probe._load_env_file(str(env_file))

    assert loaded == (
        "LBX_SRC_DATABASE",
        "LBX_SRC_HOST",
        "LBX_SRC_PASSWORD",
        "LBX_SRC_USER",
    )
    assert os.environ["LBX_SRC_HOST"] == "server.database.windows.net"
    assert os.environ["LBX_SRC_DATABASE"] == "migration db"
    assert os.environ["LBX_SRC_USER"] == "sqladmin"
    assert os.environ["LBX_SRC_PASSWORD"] == "p@ss#word=still-the-password"


def test_env_file_loader_reports_the_bad_line_without_echoing_its_value(tmp_path):
    env_file = tmp_path / "azure_sql.env"
    env_file.write_text("LBX_SRC_HOST\n")

    with pytest.raises(SystemExit, match=r"azure_sql\.env:1: expected NAME=VALUE"):
        probe._load_env_file(str(env_file))
