from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    """Today's date in Korea; air dates are Korean broadcast dates."""
    return datetime.now(KST).date()


def kst_date(utc: datetime) -> date:
    """The date in Korea at a naive-UTC time (the app-wide timestamp convention)."""
    return utc.replace(tzinfo=timezone.utc).astimezone(KST).date()
