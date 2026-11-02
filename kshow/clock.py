from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    """Today's date in Korea; air dates are Korean broadcast dates."""
    return datetime.now(KST).date()
