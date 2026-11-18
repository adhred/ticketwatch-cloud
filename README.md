# ticketwatch

Alerts you when BookMyShow or District opens bookings for a movie at the theatres you pick.

## Setup

1. Find your theatre ids (search words match the start of words in the name):
   ```
   ./tw theatres "amb cinemas"
   ./tw theatres prasads
   ./tw theatres inorbit
   ```
   Some theatres are only on one app (AMB and Prasads are BookMyShow-only), so add both ids when a theatre is on both.
2. Edit `config.toml`: city, movie, optional language/format/date filters, theatre ids.
3. Optional phone alerts: copy `secrets.example.toml` to `secrets.toml`, fill it in, then `chmod 600 secrets.toml`.
4. Try it: `./tw check --dry-run` prints what it would alert on without sending anything.
5. Test alerts: `./tw test-alert`
6. Run in the background: `./tw install` (checks every `check_every_minutes` while the Mac is awake). Stop with `./tw uninstall`.

## How it works

- **BookMyShow:** `api/v3/mobile/showtimes/byvenue` returns a theatre's open dates and that day's shows. One request per theatre, plus one per open date you care about.
- **District:** the cinema page (`/movies/<name>-CD<id>?fromdate=YYYY-MM-DD`) embeds the sessions as JSON.
- Requests copy Chrome's network fingerprint (`curl_cffi`) so Cloudflare doesn't block them. A refused request is retried once as Safari. Requests are spaced 1.5–3 s apart, and a site that keeps refusing is paused for 5, 10, 20, then up to 60 minutes.
- You get one alert per theatre, movie and date the first time shows appear, with the showtimes, seat status and a booking link. New dates alert again.
- If a site fails 3 checks in a row, you get a "checks failing" alert so that silence doesn't look like "not open yet". You get another alert when it recovers.
- No server and no third-party service: data goes only between your Mac and the two sites, plus ntfy or Telegram if you turn them on.

## Files

- `config.toml`: what to watch
- `secrets.toml`: ntfy topic and Telegram token (not required)
- `state.json`: alerts already sent; delete it to get every alert again
- `logs/ticketwatch.log`: background job log

## Limits

- Neither app has a public API, and both may change their pages and break this. The failure alert tells you when that happens.
- Automated access is against both sites' terms. Keep the interval at 5 minutes or more and use this only for yourself.
- The background job only runs while the Mac is awake.
