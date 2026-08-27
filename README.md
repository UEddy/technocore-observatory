# Technocore Observatory

A sampling-based record of the technocore.chat network: what it publishes about
itself, tracked over time.

## Methodology

The sampler makes one `GET https://technocore.chat/rooms` call per sampling
interval and writes the response body to an append-only NDJSON archive,
verbatim. Nothing is fetched per room, and the room list is never crawled or
enumerated. Coverage builds up over time from the rolling 50 room window the
endpoint returns, which is biased toward currently active rooms and is labelled
that way wherever it is presented.

Room names and topics are self-asserted by whoever created or annotated them.
They are unverified strings. This project stores them and reports on their
shape; it does not treat them as claims about what a room is or who runs it.

## Request budget

- One request per sampling interval, default interval 5 minutes
- Absolute ceiling of 30 requests per hour across all endpoints
- One sequential worker, never concurrent requests
- On 429: honor `Retry-After` and any bucket details in the response body
- On 503: exponential backoff starting at 60 seconds, capped at 30 minutes
- A retry is never scheduled tighter than the interval that preceded it

The budget and the backoff position are both derived from the archive itself,
not from a side-car state file, so every request the tool has ever made is in
the committed record and the ceiling can be audited by anyone who clones the
repo.

## Status

Build step 1 of 6 is done: the fetcher, backoff, and the raw NDJSON archive.
There is no parser yet, by design. Parsing happens in step 2, reading the
archive; the fetcher stays unaware of the response format so that a format
change can never cost a snapshot.

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

One JSON object per line in `data/raw/rooms.ndjson`, one line per request
attempt, successes and failures alike:

| field | meaning |
| --- | --- |
| `schema` | record shape version |
| `fetched_at` | UTC timestamp of the attempt |
| `url`, `source` | what was requested and where the response came from |
| `ok`, `http_status`, `error` | outcome of the attempt |
| `headers`, `elapsed_ms` | response headers and round trip time |
| `body` | the response body, whole and unmodified |
| `body_bytes`, `body_sha256` | length and digest of the original bytes |
| `body_base64` | present only when the body is not valid UTF-8 |
| `backoff_seconds` | wait applied after a failed attempt, else null |
| `parse_version` | reserved for step 2, always null here |

Failed attempts are recorded too. They cost budget, and leaving them out would
make the archive an incomplete account of what the tool did.

## License

Not yet chosen: MIT or Apache-2.0, per the build spec.
