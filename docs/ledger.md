# The ledger: what it guarantees, and how to keep it

Finrail's selling point is a run ledger a fund can audit: every model call, every
proposal, every approval and every result is written down before and after it happens.
This page says what that record can and cannot prove, how to check it, and what an
operator has to do so a restart does not erase it.

## What is written

Everything lives under the state directory (`FINRAIL_STATE`, `/data/state` in the
container):

| Path | What it holds |
| --- | --- |
| `events/<thread>.jsonl` | One line per event in a conversation: messages, model calls, runs made, updated, approved and dropped. Runs made outside any conversation go to `events/_ledger.jsonl`. |
| `events/<thread>.jsonl.head` | The seq and hash of the last line written to that log. |
| `events/blobs/` | System prompts, stored once under their digest. |
| `runs/<id>.json` | A run's current record: proposal, status, verdict, result. |
| `runs/<id>.history.jsonl` | Every earlier version of that record, in order, and its final state when it was dropped. |
| `runs/<id>.history.jsonl.head` | The seq and hash of the last history line. |
| `ledger-v1-backup/` | The pre-chain files, kept by `finrail ledger migrate`. |

## The chain

Every line written since schema 2 carries three chain fields:

- `prev`: the SHA-256 of the line before it, exactly as stored on disk. The first line
  of a file carries a fixed genesis value (64 zeros).
- `hash`: the SHA-256 of the line's own canonical JSON (keys sorted, compact separators,
  UTF-8), with `prev` included and `hash` and `mac` left out.
- `mac`: present only when the install has a ledger key. HMAC-SHA256 of `hash` under
  that key.

Lines are written in canonical form, so the bytes on disk are the bytes the hash
covers. `seq` still counts from 1 per file with no gaps. The `.head` sidecar beside each
log records the last seq and hash written (and its own HMAC when there is a key).

Run records chain the same way: before a record is overwritten, the version about to
be replaced is appended to its history file as a chained line. A drop appends the final
record with its verdict, the reason and who dropped it, and only then removes the
current file. Nothing is deleted without its last state being kept.

Approval is in the chain too. When a run is made, its `run` event carries the proposal
and its digest. When a person approves it, an `approval` event records the digest they
approved. Approval checks the run file's proposal against the chained `run` event, and
the worker checks what it is handed against the chained approval. An edit to the run
file, even one that rewrites `proposal_sha256` beside the proposal, does not reach the
chain, so it is refused.

## What that proves

| Someone with disk access ... | Without a key | With a key |
| --- | --- | --- |
| edits a line | seen: its hash no longer matches | seen |
| deletes or inserts a line | seen: the next line's `prev` and `seq` no longer match | seen |
| cuts the tail off a file | seen: the head records a later seq | seen |
| cuts the tail off, then lets the service append again | seen: the next line records what the head said (`found_break`), hashed with the rest | seen |
| empties a file to nothing | seen: the head records a seq over an empty file | seen |
| rewrites a line and recomputes every hash and the head after it | not seen | seen: they cannot produce the HMACs |
| deletes a whole file and its head | not seen by `verify`; `compare` reports a run whose log is gone | same |
| edits a run file and its stored hash before approval | refused at approval, against the chain | refused |

Put plainly: without a key the chain catches mistakes, crashes and casual edits, and
makes a deliberate rewrite a job that has to recompute everything after the edit.
With a key, a deliberate rewrite needs the key. The model, the browser and the console
never see the key, so nothing they write can forge a line.

What the chain does not do:

- It does not prove *when* a line was written. Timestamps are what the writer said.
- It does not stop someone with the key from rewriting history. Keep the key away from
  the data disk (below).
- It does not notice a file that is removed entirely, together with its head. Back the
  state up (below), and compare backups.
- A pre-v2 line (no chain fields) is accepted and reported as "unchained, pre-v2". Only
  `finrail ledger migrate` chains those.
- A torn line (a write cut off by a crash, which does not parse) is stepped over and
  listed, not counted as damage: the appender steps over it the same way, so the chain
  on either side of it still holds.

## Checking it

```bash
finrail ledger verify
```

Walks every event log and every run history, checks `seq`, `prev`, `hash` and the HMAC
when a key exists, compares each file to its head, and reports the first break per file
with its line number. It also runs the older `compare`, which says where the log and the
run files tell different stories. Exit code 1 on any break, 0 otherwise. `--json` prints
the full report. Run it from cron, and before any audit.

## The key

Set one before the first real run, or run `finrail ledger migrate` once right after
setting it, so every line is signed.

- Environment: `FINRAIL_LEDGER_KEY`. This is the way on Cloud Run: store the key in
  Secret Manager and expose it as an environment variable on the service, so it never
  sits on the data volume.
- Secret store: `finrail secret set ledger-hmac-key` (the value is read from stdin).
  On a Mac that is the keychain. On Linux the default file store is
  `<state>/secrets.json`, which is the same disk as the ledger: anyone with disk access
  then has the key too, and the HMAC adds nothing over the chain. Use the environment
  there.

A key is read once per process. Verify tells you which guarantee it applied in its
first line.

The key is install-level. There is no per-user signing; who approved what is recorded
as data in the `approval` event, not proved by a signature.

## Migrating a pre-chain state directory

```bash
finrail ledger migrate
```

Copies `events/*.jsonl` and `runs/*.json` to `<state>/ledger-v1-backup/`, rewrites each
log with every line chained from genesis (signed when a key is set), and marks every
run record `schema: 2`. Each line keeps its own seq, time and content; only the chain
fields are added. A line that does not parse is left out and counted; the original is
in the backup. The command refuses to run twice so the backup always holds the real
pre-chain state. New lines appended to an unmigrated file chain onto the old ones from
that point, so migration is only needed to cover the lines written before this
version.

## Durability on Cloud Run

The container keeps state on its own disk, so a restart or a new revision starts with
an empty ledger. Mount `/data` on something that outlives the instance.

**Cloud Storage with GCS FUSE** (the simplest; fine for one instance):

```bash
gcloud run services update finrail-console \
  --add-volume name=state,type=cloud-storage,bucket=YOUR_BUCKET \
  --add-volume-mount volume=state,mount-path=/data \
  --set-env-vars FINRAIL_STATE=/data/state,FINRAIL_HOME=/data/home
```

Notes: GCS FUSE does not support file locks, and the ledger locks each log while it
appends. Run a single instance (`--max-instances=1`) so no two processes append to the
same file; with one process the locks are only taken within that process and the
chain stays ordered. Writes are slower than local disk, which shows on long runs that
record many steps.

**A persistent disk** (faster, supports locks; needs the second generation runtime):

```bash
gcloud compute disks create finrail-state --size=10GB --zone=ZONE
gcloud run services update finrail-console --execution-environment=gen2 \
  --add-volume name=state,type=persistent-disk,disk=finrail-state \
  --add-volume-mount volume=state,mount-path=/data \
  --set-env-vars FINRAIL_STATE=/data/state,FINRAIL_HOME=/data/home
```

A read-write persistent disk can be attached to one instance only, so keep
`--max-instances=1`.

Either way, put the ledger key in Secret Manager and expose it as `FINRAIL_LEDGER_KEY`
on the service, not on the volume.

## What to back up

- The whole state directory: `events/` (logs, heads and `blobs/`), `runs/` (records,
  histories and heads), `settings.json`, and `finance/` where released memos land.
  A nightly copy of the bucket or a disk snapshot is enough.
- The ledger key, in Secret Manager or wherever your secrets live. Without it an old
  backup still verifies as "chain only".
- `ledger-v1-backup/` once, after a migration.

Run `finrail ledger verify` against a restored copy (`FINRAIL_STATE=<copy> finrail
ledger verify`) to know the backup is whole before relying on it.
