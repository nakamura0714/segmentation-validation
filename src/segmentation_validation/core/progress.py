"""走査の進捗表示。

tqdm を使わないのは、5〜8分の走査をログへリダイレクトしたときに
キャリッジリターンの塊になって読めなくなるから。
``logging`` に流せば端末でもログファイルでも同じように読める。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class Progress:
    """一定件数ごと、または一定秒数ごとに進捗を1行出す。"""

    def __init__(
        self,
        total: int,
        label: str = "処理",
        every: int = 100,
        seconds: float = 10.0,
    ) -> None:
        self.total = total
        self.label = label
        self.every = max(every, 1)
        self.seconds = seconds
        self.done = 0
        self._started = time.monotonic()
        self._last_report = self._started

    def advance(self, count: int = 1) -> None:
        self.done += count
        now = time.monotonic()
        if (
            self.done % self.every == 0
            or now - self._last_report >= self.seconds
            or self.done >= self.total
        ):
            self._last_report = now
            self._report(now)

    def _report(self, now: float) -> None:
        elapsed = now - self._started
        rate = self.done / elapsed if elapsed > 0 else 0.0
        remaining = (self.total - self.done) / rate if rate > 0 else 0.0
        logger.info(
            "%s %d/%d (%.0f%%) %.1f件/秒 残り %s",
            self.label,
            self.done,
            self.total,
            100.0 * self.done / self.total if self.total else 100.0,
            rate,
            _humanize(remaining),
        )

    def finish(self) -> None:
        elapsed = time.monotonic() - self._started
        logger.info("%s 完了 %d件 / %s", self.label, self.done, _humanize(elapsed))


def _humanize(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{seconds:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}時間{minutes:02d}分"
