"""Can a cloud runner reach BookMyShow and District? Prints status per request."""
from datetime import datetime, timedelta, timezone

from curl_cffi import requests

IST = timezone(timedelta(hours=5, minutes=30))
day = datetime.now(IST).strftime("%Y%m%d")
tests = {
    "bms byvenue PRHN": ("https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue",
                         {"venueCode": "PRHN", "regionCode": "HYD", "dateCode": "20261217", "appCode": "WEB"}),
    "bms byvenue ALUC": ("https://in.bookmyshow.com/api/v3/mobile/showtimes/byvenue",
                         {"venueCode": "ALUC", "regionCode": "HYD", "dateCode": day, "appCode": "WEB"}),
    "district cinema": ("https://www.district.in/movies/cinema-CD1022263", None),
}
ok = 0
for browser in ("chrome", "safari"):
    s = requests.Session(impersonate=browser)
    for name, (url, params) in tests.items():
        try:
            r = s.get(url, params=params, timeout=20,
                      headers={"Accept": "application/json", "x-app-code": "WEB", "x-region-code": "HYD"})
            good = r.status_code == 200 and ("ShowDetails" in r.text or "__NEXT_DATA__" in r.text)
            ok += good
            print(f"{browser:7} {name:18} HTTP {r.status_code} {'OK' if good else 'BLOCKED/odd: ' + r.text[:80]!r}")
        except Exception as e:
            print(f"{browser:7} {name:18} ERROR {e}")
print(f"{ok}/{2 * len(tests)} succeeded")
