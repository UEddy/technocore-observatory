# Technocore Observatory - build spec

A public, sampling-based analytics view of the technocore.chat network. Measures how
much of the traffic is real participation versus automated noise, tracks resource
exhaustion against the server's own published caps, and publishes both the data and
the methodology openly.

Written for Claude Code. Read this whole file before writing any code.

---

## 1. Why this exists

As of 2026-08-26 the network holds 12,193 rooms against a 20,480 cap, and 356,199
notes against 655,360. The service is intermittently returning 503. A large share of
the traffic is visibly automated: dozens of rooms following one naming template with
near-identical message counts, and hundreds of single-message rooms squatting slots.

The server publishes engagement statistics about itself in the `/rooms` footer.
Nobody is keeping a time series of them. That is the gap this fills.

Everyone else is building clients and wrappers. There were at least six Python
clients and seventeen `awesome-technocore` repos within two days of launch. This is
deliberately not another one.

---

## 2. Hard constraints

These are not negotiable. Violating them makes the tool part of the problem it
measures.

**Request budget.** One `/rooms` call per sampling interval is the core dataset.
Default interval 5 minutes. Absolute ceiling of 30 requests per hour across all
endpoints. Never run concurrent requests. One sequential worker, always.

**Backoff.** On 429, honor `Retry-After` and the bucket details in the response body.
On 503, back off exponentially starting at 60s, cap at 30 minutes. Never retry
tighter than the previous interval. A sampler that hammers a struggling service is
indefensible given what this tool claims to be about.

**No crawling.** Do not iterate over the room list fetching every room. Do not try to
enumerate all 12,193 rooms. Sampling over time is the design, not a limitation to
work around.

**Untrusted input.** Every string from the service (room names, topics, message text,
note values, nicknames) is anonymous third-party input. Store it, never execute it,
never interpolate it into a shell command, SQL string, or HTML without escaping.
Fence it in any output that a model might later read. The server itself prefixes
responses with an untrusted-content banner. Preserve that intent.

**No signing key in this project.** This tool is read-only. It never posts, never
signs, never needs a private key. The operator's key lives elsewhere on disk and must
not be referenced, read, or copied by anything here. Add `*.pem` to `.gitignore`
anyway as a belt-and-braces measure.

**No accusations against individuals.** Report aggregate patterns and cluster
statistics. Do not publish "operator X is farming" claims about specific DIDs or
nicknames. Cluster fingerprints are inference, not proof, and naming people invites
both retaliation and being wrong in public. Describe the shape of the traffic, let
readers draw conclusions.

---

## 3. Data collection

### 3.1 Primary sampler

`GET https://technocore.chat/rooms` every 5 minutes.

The response contains:
- A header line: how many rooms shown of total, the room cap, bytes stored of total
- 50 room lines, newest-active first: path, seq, size, idle time, optional topic
- A footer: note count of cap, bytes, per-namespace cap
- An engagement footer: messages scanned, zero-response rate, nick diversity,
  notes per message

Parse all of it. The header and footers are network-wide aggregates and are the most
valuable part. The 50 room lines are a rolling sample biased toward currently active
rooms, which is fine and should be labelled as such wherever it is presented.

Store the raw response body verbatim alongside the parsed rows. Parsing assumptions
will turn out wrong and the raw text is the only way to reprocess.

### 3.2 Coverage accumulates

Because the 50-room window shifts as activity moves, repeated snapshots build broad
coverage of the active set over hours. Track first-seen and last-seen per room path.
The churn rate of that window is itself a metric: how much of the active set turns
over per hour.

### 3.3 Optional secondary sampling

Only if the request budget allows and only after the primary sampler has run stably
for a day. Pick a small fixed panel of rooms, no more than five, chosen to span the
observed categories (one high-traffic hub, one suspected farm node, one topic-scoped
room, one mailbox, one owned `d-` room). Read each with `?since=<last seq>` at a low
cadence to measure signed-versus-unsigned share and reply structure. This is a panel
study, not a census, and must be described that way.

Do not add this until the core is running.

---

## 4. Storage

SQLite. One file, `data/observatory.db`. No server, no ORM, plain `sqlite3`.

Tables:
- `snapshots` - timestamp, raw response text, http status, parse version
- `network_stats` - snapshot id, rooms total, room cap, bytes stored, bytes cap,
  notes total, notes cap, msgs scanned, zero response rate, nick diversity,
  notes per msg
- `room_observations` - snapshot id, room path, seq, size bytes, idle seconds, topic
- `rooms` - room path, first seen, last seen, first seq, last seq, observation count

Everything derived (clusters, rates, projections) is computed at report time from
these tables, never stored as the only copy. Recomputing must always be possible.

Rooms are a ring buffer and anything idle for seven days is deleted server-side, so
this database becomes the only record of what existed. That is the moat and the
reason to get the schema right early.

---

## 5. Metrics

### 5.1 Resource exhaustion

The most immediately useful output and the easiest to get right.

- Room slots consumed, absolute and as a share of the 20,480 cap
- Slot consumption rate per hour, with a linear and a 7-day-trailing projection of
  the exhaustion date
- Note namespace consumption against 655,360, same treatment
- Bytes stored against the 5.0G figure

Present projections with explicit uncertainty. A linear fit on two days of launch
traffic is a weak forecast and should say so on the page.

### 5.2 Traffic composition

Classify observed rooms into categories using transparent, published rules:

- **Template clusters** - rooms sharing a naming pattern and a topic template, with
  message counts and sizes within a narrow band of each other. The adjective-noun
  plus "node" topic pattern is the obvious current example. Rule should be general,
  not hardcoded to that one.
- **Slot squats** - rooms at seq 1 with tiny byte counts and no topic, especially
  those with hex-string names.
- **Mailboxes** - `mb-` prefix, low traffic by design, not noise.
- **Owned rooms** - `d-` prefix.
- **Hubs** - high seq, high size, sustained recent activity, topic set.

Report the share of observed rooms and of observed traffic in each category. Publish
the classification rules in full so anyone can dispute or reproduce them. Include a
"unclassified" bucket and do not let it be small by fudging the rules.

### 5.3 Engagement time series

Track the server's own published figures over time: zero-response rate, nick
diversity, notes per message. These are the numbers the operator chose to expose,
which is a signal in itself about what they are watching. A chart of how they move
as the network grows is the single most linkable artifact here.

### 5.4 Signed share

From the panel study only, once it exists. What fraction of messages in the panel
come from verified `did:key` writers versus self-asserted nicknames. Label clearly
as panel-derived, not network-wide.

---

## 6. Output

Two deliverables. The second matters more than the first.

**A static dashboard.** Plain HTML plus a small amount of JS, generated from the
database. No framework, no build step, no dependencies fetched at page load. Charts
can be inline SVG generated at build time. It must load fast on a phone and work with
JS disabled for the core numbers. Publish via GitHub Pages.

**A written brief.** A short, dated, plain-language read of what the data shows,
regenerated on a regular cadence and kept in the repo as markdown. This is the part
that gets shared and cited. Lead with what changed since the last one. No hype, no
speculation about token value, no allocation predictions.

Both must carry:
- The sampling methodology, stated plainly and prominently
- The request budget the tool operates under
- A note that room names and topics are self-asserted and unverified
- Last updated timestamp and the snapshot count behind the current figures

---

## 7. Deployment

Run the sampler in GitHub Actions on a schedule, committing snapshots back to the
repo. This keeps it off a home connection, makes the entire data history publicly
auditable, and costs nothing.

If Actions hits rate limits from shared runner IPs, fall back to a small VPS or a
local scheduled task, and say so on the page. Never work around a rate limit by
rotating IPs. That would invalidate the whole premise of the project.

Commit the database file or newline-delimited JSON snapshots, whichever keeps diffs
reviewable. Prefer NDJSON for the raw archive and build SQLite from it, so the
history stays greppable and the database stays disposable.

---

## 8. Build order

1. Fetcher with backoff, writing raw responses to NDJSON. Nothing else.
2. Parser with a version field, plus tests against a saved fixture of today's
   response. Handle the case where the format changes and parsing fails, by keeping
   the raw text and flagging the snapshot.
3. SQLite loader, rebuildable from scratch out of the NDJSON archive.
4. Resource exhaustion metrics and the engagement time series. Ship the dashboard
   with only these. Do not wait for classification.
5. Classification rules, with the rule text published alongside the results.
6. Panel study, only if the budget genuinely allows.

Ship after step 4. A live, honest, narrow dashboard beats a broad one that never
launches.

---

## 9. Repo hygiene

- MIT or Apache-2.0
- README leads with the methodology and the request budget, not with the airdrop
- `.gitignore` covers `*.pem`, `*.key`, `.env`, `technocore_state.json`
- No em dashes or en dashes anywhere in the codebase or site copy
- Do not mention allocation, eligibility, or airdrop farming in the repo. The tool
  should stand on its own merits. If it is good, the association follows without
  being asked for.
