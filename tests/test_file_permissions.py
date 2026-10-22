from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_auth import load_web_control_module


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class FilePermissionTests(unittest.TestCase):
    """NWR Stream Manager's files can be read and changed by the account it runs as, and nobody else."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.state_files = sys.modules["nwr_stream_manager.state_files"]

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)

    def test_files_saved_before_are_made_private(self) -> None:
        folder = self.root / "nwr-stream-manager"
        recordings = folder / "iq-recordings"
        recordings.mkdir(parents=True)
        files = [folder / "accounts.db", folder / "notifications.json", folder / "rtl-control.json", recordings / "index.json"]
        for path in files:
            path.write_text("{}")
            path.chmod(0o644)
        recordings.chmod(0o755)
        folder.chmod(0o755)
        # Not NWR Stream Manager's, so left as it is.
        neighbour = folder / "someone-elses-notes.txt"
        neighbour.write_text("mine")
        neighbour.chmod(0o644)
        state = folder / "rtl-control.json"
        self.state_files.make_owner_only(
            self.wc.owned_state_paths(state, folder / "nwr-stream-manager.log"), folder=self.wc.owned_state_folder(state)
        )
        for path in files:
            self.assertEqual(mode(path), 0o600, path)
        self.assertEqual(mode(recordings), 0o700)
        self.assertEqual(mode(folder), 0o700)
        self.assertEqual(mode(neighbour), 0o644)

    def test_a_folder_chosen_with_state_is_left_alone(self) -> None:
        home = self.root / "home"
        home.mkdir()
        home.chmod(0o755)
        state = home / "rtl-control.json"
        state.write_text("{}")
        state.chmod(0o644)
        self.assertIsNone(self.wc.owned_state_folder(state))
        self.state_files.make_owner_only(self.wc.owned_state_paths(state, home / "nwr-stream-manager.log"))
        self.assertEqual(mode(state), 0o600)
        self.assertEqual(mode(home), 0o755)

    def test_links_are_left_alone(self) -> None:
        target = self.root / "elsewhere.json"
        target.write_text("{}")
        target.chmod(0o644)
        link = self.root / "notifications.json"
        link.symlink_to(target)
        self.state_files.make_owner_only([link])
        self.assertEqual(mode(target), 0o644)

    def test_new_files_are_private_from_the_start(self) -> None:
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        folder = self.root / "nwr-stream-manager"
        state = folder / "rtl-control.json"
        log = folder / "nwr-stream-manager.log"
        # Stops once the log is set up, which is the first file it writes.
        with mock.patch.object(self.wc, "check_startup_dependencies", side_effect=RuntimeError("stop here")), \
                mock.patch.object(self.wc, "configure_file_logging", side_effect=lambda path: (folder.mkdir(), path.write_text(""))):
            with self.assertRaises(RuntimeError):
                self.wc.run_server(None, None, state, log_file=log)
        self.assertEqual(os.umask(0o022), self.state_files.OWNER_ONLY_UMASK)
        self.assertEqual(mode(log), 0o600)
        self.assertEqual(mode(folder), 0o700)


if __name__ == "__main__":
    unittest.main()
