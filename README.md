# fke-pull

Pulls your bookings out of the Fun Kids Entertainers portal and writes two CSVs:
`invoice.csv` (what you bill FKE — 20% of each fee) and `details.csv` (the
per-party information behind it).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then put your portal login in it
```

`.env` is gitignored. Credentials are only ever read from `FKE_USER` /
`FKE_PASS` in the environment or in that file — nothing is written back to disk
except the page cache and the CSVs.

## Use

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
1,100-26502,03/07/2025,party,120.50,24
2,100-26503,21/11/2025,school disco,240.00,48
TOTAL,,,,360.50,72
```

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

Also in there: the percentage and how it's rounded (`half_up` — 32.50 becomes
33, the way you'd do it by hand), the cache settings, and the portal paths.

## Things this tool is deliberately careful about

**The date range is US format.** `DateRange` on `/Bookings/MyBookingHistory` is
`MM/DD/YYYY` even though everything the portal displays is UK. Sending UK order
doesn't error, it just quietly returns fewer bookings. It's built in one place
(`fkepull/dates.py`, `format_search_date`) and isn't configurable.

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
anti-forgery token, a `DateRange` that's parsed as US format and silently
matches nothing otherwise, and detail pages marked up three different ways. It
covers the rounding, the theme mapping, the cache, the 5-request cap, and each
of the failure modes above.

## Not yet verified against the live site

This was built and tested without network access to
`funkidsentertainers.bykayo.digital` (blocked at the network level from where it
was written), so the HTML shapes it handles are modelled from the description of
the site rather than observed. The parsing is written to accept several common
ASP.NET MVC layouts and to fail loudly rather than guess, so the first live run
will either work or tell you exactly what it didn't recognise.

Worth doing on the first real run:

1. `./fke-pull -v --since <a month you remember well>` and check the row count,
   the fees, and the dates against the portal by eye.
2. If login fails, set `login_path` under `[site]` in `config.toml` to the real
   login URL (auto-detection follows the redirect from `/`).
3. If a detail field comes out blank that shouldn't be, the label spelling on
   the real page probably differs from the ones listed in
   `DETAIL_LABELS` in `fkepull/parse.py` — add it there.
