import errno
import threading
import time
from pathlib import Path

import pytest
from watchdog.observers.polling import PollingObserver

from ai_marketplace_monitor import utils


@pytest.mark.parametrize("error", [errno.EMFILE, errno.ENOSPC])
def test_inotify_quota_falls_back_and_still_detects_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("before")
    stopped = []

    class ExhaustedObserver:
        def schedule(self, *args, **kwargs) -> None:
            raise OSError(error, "inotify quota reached")

        def stop(self) -> None:
            stopped.append(True)

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(utils, "Observer", ExhaustedObserver)
    monkeypatch.setattr(
        utils, "PollingObserver", lambda: PollingObserver(timeout=0.05), raising=False
    )

    def edit() -> None:
        time.sleep(0.15)
        config.write_text("after")

    writer = threading.Thread(target=edit)
    writer.start()
    try:
        assert utils.doze(3, [config]) == utils.SleepStatus.BY_FILE_CHANGE
        assert stopped == [True]
    finally:
        writer.join()
