# fke-pull

Pulls your bookings out of the Fun Kids Entertainers portal. Two ways to use it:

- **The app** — double-click, and past and upcoming bookings open in your
  browser as two tabs, with your 20% totalled on each and CSV downloads.
- **The command line** — writes `invoice.csv` and `details.csv` and exits.

Both do exactly the same pull, so they always show the same numbers.

## Setup

```bash
cp .env.example .env      # then put your portal login in it
```

`.env` is gitignored. Credentials are only ever read from `FKE_USER` /
`FKE_PASS` in the environment or in that file — nothing is written back to disk
except the page cache and the CSVs.

## The app

Double-click **`FKE bookings.command`** in Finder. The first run sets itself up
(a minute or so), then your browser opens on the bookings. Closing the Terminal
window it opens stops the app.

From a terminal it's `./fke-pull --serve`.

- **Past bookings** — everything the portal counts as done, i.e. what you can
  invoice for. Total fees and your 20% at the top; *Download invoice.csv*.
- **Upcoming** — parties still to come, with their fees and 20% totalled
  separately so next month's figure is ready before you've worked it.
- **Refresh** re-pulls, using the local cache. **Full refresh** re-reads every
  detail page from the portal.
- Click any column heading to sort. Already-invoiced bookings are tagged.

It listens on `127.0.0.1` only, so nothing outside your Mac can reach it, and
every request has to carry the one-time token in the URL it opens — otherwise
any website you happen to have open could quietly talk to it.

## The command line

```bash
pip install -r requirements.txt   # not needed if you use the launcher
```

```bash
./fke-pull                              # everything you've ever done
./fke-pull --since 01/04/2026           # just this month's work
./fke-pull --since 01/04/2026 --invoiced-refs invoiced.txt
./fke-pull --source both                # include upcoming parties too
./fke-pull -v                           # show every request
```

Dates you type are UK format (`DD/MM/YYYY`); `YYYY-MM-DD` also works.

### Options worth knowing

| Flag | What it does |
| --- | --- |
| `--since` / `--until` | Limit by event date. Also narrows the portal-side search. |
| `--source` | `completed` (default), `upcoming`, or `both`. |
| `--invoiced-refs FILE` | Refs you've already billed, one per line, `#` comments allowed. |
| `--invoiced-mode` | `exclude` (default) leaves them out; `mark` keeps them with an extra column and totals both ways. |
| `--refresh-cache` / `--no-cache` | Re-fetch detail pages instead of using the local copies. |
| `--max-concurrency` | Fewer than 5 if you want to be gentler. Never more than 5. |
| `--allow-blank-fees` | Carry on when a booking has no Balance, listing it with an empty fee. |
| `--out-dir`, `--invoice-csv`, `--details-csv` | Where things get written. |
| `--serve`, `--port`, `--no-open` | Run the app instead of writing CSVs. |

### The refs file

```text
# invoiced March 2026
100-26501
100-26502
```

After each run, append the refs from `invoice.csv` to this file and the next
run won't bill them again. The run also tells you if a ref in the file wasn't
in the batch at all, so you can spot a booking that's gone missing.

## Output

`invoice.csv` — row number, booking ref, date (DD/MM/YYYY), theme, fee, 20%,
and a total row.

```csv
#,Booking Ref,Date,Theme,Fee,20%
1,100-26502,03/07/2025,party,120.50,24.10
2,100-26503,21/11/2025,school disco,240.00,48.00
TOTAL,,,,360.50,72.10
```

The 20% is exact to the penny. The only rounding that ever happens is to two
decimal places, when the percentage lands on a fraction of a penny (20% of
£99.99 is £19.998, invoiced as £20.00).

`details.csv` — booking ref, booking confirmed date, customer name, phone,
package, venue (address line 1), party occasion, duration.

Both are sorted by event date ascending and written with a BOM so Excel opens
them as UTF-8.

## config.toml

The bit you'll edit is `[themes]`, which turns package names into the short
labels on the invoice:

```toml
[themes.exact]
"FUN Kids Party (Indoor Use)" = "party"
"FUN School Disco" = "school disco"
```

Anything not matched exactly falls through to the `[[themes.rules]]` regexes
below it. The first rule handles themed parties generically, so
`Fun Kids Party + Theme / Batman` becomes `batman` without you touching the
file. A package that matches nothing at all is listed in a warning at the end
of the run and gets its full name as the theme, so it's obvious and easy to
fix. Set `on_unknown = "fail"` if you'd rather it stopped instead.

Also in there: the percentage, the cache settings, and the portal paths.

## Things this tool is deliberately careful about

**The date range format is proved, not assumed.** `DateRange` on
`/Bookings/MyBookingHistory` is the one field where a wrong guess doesn't error
— it just quietly returns fewer bookings, so the run "succeeds" with lines
missing off the invoice. Rather than trust anyone's memory of which way round it
goes, the first run sends the same search both ways and keeps whichever finds
more bookings, then remembers the answer in the cache directory. It re-checks
automatically whenever a search comes back empty, so if FKE ever change it, the
tool notices instead of silently under-billing. Pin it with
`search_date_format = "MDY"` (or `"DMY"`) under `[site]` if you'd rather.

**Dates the portal shows are checked, not assumed.** `14/06/2026` proves the
display format is day-first; if a batch has no date past the 12th of a month,
the tool fetches a wider range to find one. Only if that still isn't enough
does it stop and ask you to set `display_date_format` — because reading them the
wrong way round would silently drop bookings from a `--since` filter.

**Detail pages are checked against the ref you asked for.** The id is the ref's
number minus 10000, which is a guess about FKE's database, not a contract. Every
page is checked against the ref that was requested and the run stops if they
ever disagree. The event date on the detail page is also cross-checked against
the one in the table.

**Fees are never invented.** A balance that can't be read as money is an error,
not a zero. A blank balance stops the run and names the bookings, unless you
pass `--allow-blank-fees`. The customer deposit FKE take doesn't appear on these
pages and plays no part in the maths.

**A changed page layout stops the run.** If the table loses a column, or the
`Balance` label disappears, you get a message saying what was expected and what
was actually on the page — not a plausible-looking invoice with a hole in it.

**Empty fields stay empty.** Several detail fields are usually blank. The parser
treats the element structurally next to a label as that label's value even when
it's empty, so a blank `Address Line 2` can't quietly pick up the town below it.

## Caching and load on FKE's server

Detail pages for parties that have already happened are cached forever under
`.fke-cache/` (gitignored), so a repeat run costs two requests plus whatever is
new. Pages for future bookings are re-fetched after 6 hours since they can still
change. Never more than 5 requests run at once, and `fetch.request_delay` in
config adds a pause between them if you want to be gentler still.

## Tests

```bash
python3 -m pytest tests -q
```

The suite runs the whole tool end to end against a fake portal
(`tests/fake_portal.py`) that imitates the real one: ASP.NET MVC login with an
anti-forgery token, a `DateRange` that silently matches nothing unless it's sent
in the order that portal wants (the tests run it both ways), and detail pages
marked up three different ways. It covers the maths, the theme mapping, the
cache, the 5-request cap, the web app's own endpoints and its access controls,
and each of the failure modes above.

## Not yet verified against the live site

This was built and tested without network access to
`funkidsentertainers.bykayo.digital` (blocked at the network level from where it
was written), so the HTML shapes it handles are modelled from the description of
the site rather than observed. Everything else — the app, the maths, the CSVs,
the caching, the failure handling — is verified against the fake portal. The
parsing is written to accept several common ASP.NET MVC layouts and to fail
loudly rather than guess, so the first live run will either work or tell you
exactly what it didn't recognise.

Worth doing on the first real run:

1. `./fke-pull -v --since <a month you remember well>` and check the row count,
   the fees, and the dates against the portal by eye.
2. If login fails, set `login_path` under `[site]` in `config.toml` to the real
   login URL (auto-detection follows the redirect from `/`).
3. If a detail field comes out blank that shouldn't be, the label spelling on
   the real page probably differs from the ones listed in
   `DETAIL_LABELS` in `fkepull/parse.py` — add it there.
