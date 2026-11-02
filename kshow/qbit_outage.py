"""Notify once when qBittorrent stops working, and once when it works again.

The outage is kept in the "qbit_outage_since" setting (naive-UTC ISO time, "" when none), so
the check and the download poller share it and a restart doesn't notify again.
"""

from sqlalchemy.orm import Session

from kshow.db import get_setting, set_setting, utcnow
from kshow.notify import Notifier

KEY = "qbit_outage_since"
_MAX_REASON = 160


def _short_reason(error: Exception) -> str:
    reason = str(error).strip().rstrip(".") or type(error).__name__
    reason = reason.removeprefix("qBittorrent ")
    if len(reason) > _MAX_REASON:
        reason = reason[: _MAX_REASON - 1] + "…"
    return reason


async def qbit_failed(session: Session, notifier: Notifier, error: Exception) -> None:
    """Record a qBittorrent failure; notify only when it starts an outage."""
    if get_setting(session, KEY):
        return
    set_setting(session, KEY, utcnow().isoformat())
    await notifier.send(
        "qBittorrent problem",
        f"kshow can't reach or log in to qBittorrent: {_short_reason(error)}. "
        "Downloads are paused until it works again.",
    )


async def qbit_ok(session: Session, notifier: Notifier) -> None:
    """Record a successful qBittorrent call; notify when it ends an outage."""
    if not get_setting(session, KEY):
        return
    set_setting(session, KEY, "")
    await notifier.send("qBittorrent back", "kshow can reach qBittorrent again.")
