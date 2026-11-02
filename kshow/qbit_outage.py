"""Notify once when qBittorrent stops working, and once when it works again.

The outage is kept in the "qbit_outage_since" setting (naive-UTC ISO time, "" when none), so
the check and the download poller share it and a restart doesn't notify again. Each change is
a conditional UPDATE, so of two concurrent runs only the one that flips the flag notifies.
"""

from sqlalchemy import update
from sqlalchemy.orm import Session

from kshow.db import Setting, utcnow
from kshow.notify import Notifier

KEY = "qbit_outage_since"
_MAX_REASON = 160


def _short_reason(error: Exception) -> str:
    reason = str(error).strip().rstrip(".") or type(error).__name__
    reason = reason.removeprefix("qBittorrent ")
    if len(reason) > _MAX_REASON:
        reason = reason[: _MAX_REASON - 1] + "…"
    return reason


def _flip(session: Session, outage: bool) -> bool:
    """Start (outage=True) or end the recorded outage; True only if this call changed it."""
    if outage:
        in_other_state, new_value = Setting.value == "", utcnow().isoformat()
    else:
        in_other_state, new_value = Setting.value != "", ""
    result = session.execute(
        update(Setting)
        .where(Setting.key == KEY, in_other_state)
        .values(value=new_value)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


async def qbit_failed(session: Session, notifier: Notifier, error: Exception) -> None:
    """Record a qBittorrent failure; notify only when it starts an outage."""
    if not _flip(session, outage=True):
        return
    await notifier.send(
        "qBittorrent problem",
        f"kshow can't reach or log in to qBittorrent: {_short_reason(error)}. "
        "Downloads are paused until it works again.",
    )


async def qbit_ok(session: Session, notifier: Notifier) -> None:
    """Record a successful qBittorrent call; notify when it ends an outage."""
    if not _flip(session, outage=False):
        return
    await notifier.send("qBittorrent back", "kshow can reach qBittorrent again.")
