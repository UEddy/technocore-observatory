# Technocore Observatory

A sampling-based record of the technocore.chat network: what it publishes about
itself, tracked over time.

## Methodology

The sampler makes one `GET https://technocore.chat/rooms` call per sampling
interval, default every 15 minutes, and writes the response body to an
append-only NDJSON archive, verbatim. Nothing is fetched per room, and the room list is never crawled or
enumerated. Coverage builds up over time from the rolling 50 room window the
endpoint returns, which is biased toward currently active rooms and is labelled
that way wherever it is presented.

Room names and topics are self-asserted by whoever created or annotated them.
They are unverified strings. This project stores them and reports on their
shape; it does not treat them as claims about what a room is or who runs it.

## Request budget

- One request per sampling interval, default interval 15 minutes
- Absolute ceiling of 30 requests per hour across all endpoints
- One sequential worker, never concurrent requests
- On 429: honor `Retry-After` and any bucket details in the response body
- On 503: exponential backoff starting at 60 seconds, capped at 30 minutes
- A retry is never scheduled tighter than the interval that preceded it

The budget and the backoff position are both derived from the archive itself,
not from a side-car state file, so every request the tool has ever made is in
the committed record and the ceiling can be audited by anyone who clones the
repo. Both are read with a bounded seek to the end of the archive, so the cost
of a sample does not grow with the length of the history.

One sequential worker is enforced with a lock file that records the worker's
pid, start time and host. A lock is broken only when its holder is demonstrably
gone: the pid is not running on this host, or the lock has gone untouched for
fifteen minutes. A running loop touches its lock every cycle, so a healthy long
run is never evicted and a killed one never stops collection for more than a
sampling interval or two.

### Why 15 minutes

The whole 50 room window the endpoint returns has idle times spanning about a
minute, so the window turns over roughly once a minute. Every cadence the
30/hour ceiling permits, 2 minutes included, undersamples that churn by an
order of magnitude, so room coverage and turnover are a lower bound at any
legal interval rather than a measurement. Moving from 5 minutes to 15 makes
that lower bound looser; it does not change what kind of number it is, and the
page says so.

What it buys is worth more. The figures the dashboard ships first, resource
exhaustion and the engagement series, are network-wide aggregates that move
slowly, and 4 samples an hour serve them fully. Fifteen minutes is a third of
the request load on a service that is already returning 503, a third of the
commits and a third of the repo growth for an archive that is itself the
deliverable, and it is a cadence a shared scheduled runner can actually keep.
A nominal 5 minute schedule that lands irregularly would make the stated
methodology a fiction.

## Status

Build steps 1 to 3 of 6 are done: the fetcher with its backoff and raw NDJSON
archive, the parser that reads it, and the SQLite loader.

The parts are deliberately separate. The fetcher knows nothing about the
response format, so a format change can never cost a snapshot; the parser never
touches the network; and the database is disposable, rebuilt from the archive
whenever the parser changes.

## Running it

The default source is the saved fixture, so a development run makes no network
calls at all:

```
python -m observatory                       # one attempt against the fixture
python -m observatory --status              # budget and backoff state, no request
python -m observatory --dry-run             # what would happen, nothing written
python -m observatory --loop --interval 300 # sample on the interval
```

Exercising the backoff paths offline, still without touching the service:

```
python -m observatory --replay-status 503
python -m observatory --replay-status 429 --replay-header "Retry-After: 900"
```

Live sampling takes two explicit flags, so it cannot happen by accident:

```
python -m observatory --source http --allow-network
```

Tests, standard library only, no dependencies:

```
python -m unittest discover -s tests -t . -p "test_*.py"
```

## Archive format

One JSON object per line in `data/archive/YYYY-MM.ndjson`, one line per
request attempt, successes and failures alike:

| field | meaning |
| --- | --- |
| `schema` | record shape version |
| `fetched_at` | UTC timestamp of the attempt |
| `url`, `source` | what was requested and where the response came from |
| `ok`, `http_status`, `error` | outcome of the attempt |
| `headers`, `elapsed_ms` | response headers and round trip time |
| `body` | the response body, whole and unmodified |
| `body_bytes`, `body_sha256` | length and digest of the bytes as they arrived |
| `body_encoding`, `body_lossy` | how the body decoded, and whether anything was replaced |
| `backoff_seconds` | wait applied after a failed attempt, else null |
| `parse_version` | reserved for the loader, always null as written by the sampler |

Every record carries the same keys whatever happened to the request. Bodies are
stored one way, as text, which is what keeps the archive greppable and its
diffs reviewable. The endpoint serves UTF-8; a body that does not decode
cleanly is still stored, with the undecodable bytes replaced, and is labelled
by `body_encoding` and flagged by `body_lossy` so the anomaly is visible rather
than silent. `body_sha256` is always taken over the bytes as they came off the
wire, so a lossy record can always be proved to differ from its original.

Failed attempts are recorded too. They cost budget, and leaving them out would
make the archive an incomplete account of what the tool did.

Files rotate monthly, by the timestamp on the record rather than the clock at
write time, so a run that crosses a month boundary still files each attempt
under the month it happened in. Monthly files keep any one file small enough to
review in a diff. Nothing that reads the archive has to know where the
boundaries are: a tail read starts at the end of the newest file and walks back
into older ones only as far as it needs to.

## The database

`data/observatory.db`, built from the archive by the loader. It is disposable
and gitignored. Rebuilding it from the NDJSON is the normal way to pick up a
new parse version, and the tests assert that deleting it entirely loses
nothing.

```
python -m observatory.store            # rebuild from data/archive
python -m observatory.store --update   # add new records to an existing database
```

Four tables. `snapshots` holds one row per request attempt with the raw
response text, so everything else could be rebuilt from the database alone if
the archive were ever lost. `network_stats` holds the header and both footer
lines, including the four engagement figures. `room_observations` holds one row
per room line per snapshot. `rooms` is first-seen, last-seen, first and last
seq and an observation count per path, derived entirely from
`room_observations` and recomputed on every load, so it cannot drift from what
it summarises. First and last seq follow the snapshot timestamps rather than
the seq values, because a path can be reused after the server drops the room
and a seq can therefore go backwards.

A rebuild is written to a temporary file and moved into place, so an
interrupted build leaves the previous database untouched. Snapshots that failed
to parse, and snapshots whose body did not decode cleanly, are loaded and kept
with their raw text and counted loudly in the report; the exit status is 1 when
there are any.

Every string that came from the service is written through a bound parameter.

## Parsing

The parser reads a response body and returns a snapshot: the network-wide
aggregates, the 50 room lines, and a list of problems. It never raises. An
empty body, a truncated one, or a page from a service that has been rewritten
since this code was written all return a snapshot with whatever could be read
filled in, everything else recorded as a problem, and `flagged` set. The raw
text stays in the archive either way, so a flagged snapshot can be reparsed by
a later parse version rather than being lost.

`parse_version` is stamped on every snapshot and is bumped whenever a change to
the parser would produce different output from the same input.

The header and both footer lines are the most valuable part of the response,
because they are network-wide rather than a sample. The four engagement figures
the server publishes about itself, messages scanned, zero-response rate, nick
diversity and notes per message, are first-class fields, and their absence
flags the snapshot.

Room paths and topics are self-asserted, unverified strings from anonymous
third parties. The parser stores them exactly as they arrived and never
interprets them. A topic is a note that any caller can set on any room without
ever posting to it.

```
python -m observatory.parser                          # parse the saved fixture
python -m observatory.parser --json                   # full parse as JSON
python -m observatory.parser data/archive --archive
```

Exit status is 0 for a clean parse and 1 for a flagged one.

## License

MIT. See LICENSE.
