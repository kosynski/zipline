from zipline.utils.calendars import get_calendar,register_calendar
from .exchange_calendar_shsz import SHSZExchangeCalendar
from .exchange_calendar_hkex import HKExchangeCalendar
register_calendar("SHSZ", SHSZExchangeCalendar(), force=True)
register_calendar("HKEX", HKExchangeCalendar(), force=True)


#singleton in python
shsz_calendar =get_calendar("SHSZ")
hkex_calendar =get_calendar("HKEX")