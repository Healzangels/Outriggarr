"""The log survives a redeploy: it is also written under the config dir, rotating."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from outriggarr.main import attach_file_log


def test_the_log_is_kept_under_the_config_dir_and_rotates(tmp_path: Path) -> None:
    path = attach_file_log(tmp_path / "config")
    assert path == tmp_path / "config" / "outriggarr.log"
    logging.getLogger("outriggarr.audit").warning("a warning worth keeping")
    assert "WARNING outriggarr.audit: a warning worth keeping" in path.read_text()
    handler = next(h for h in logging.getLogger().handlers if h.get_name() == "outriggarr-file")
    assert handler.maxBytes == 2_000_000 and handler.backupCount == 4, "bounded: ten MB at most"
    # a second app in the same process re-points the one handler, never stacks another
    again = attach_file_log(tmp_path / "other")
    names = [h.get_name() for h in logging.getLogger().handlers]
    assert names.count("outriggarr-file") == 1 and again == tmp_path / "other" / "outriggarr.log"


def test_an_unwritable_config_dir_leaves_stdout_as_the_log(tmp_path: Path, caplog) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with caplog.at_level(logging.WARNING, logger="outriggarr.main"):
            assert attach_file_log(ro / "config") is None, "a warning, not a crash"
        assert "not keeping a log file" in caplog.text
    finally:
        ro.chmod(0o700)
