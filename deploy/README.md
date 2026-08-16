# Deploying the organize automation pipeline

systemd **user** units that run `organize run-consumers` on a 10-minute
cadence, per [spec 06 §5](../spec/06-automation-pipeline.md), and — the part
that matters — **report failure out loud**.

Nothing in this directory is installed or enabled automatically. Every
command below is one you run yourself.

## What is here

| File | Role |
|---|---|
| `organize-pipeline.service` | one pipeline run (`Type=oneshot`). Not enabled directly. |
| `organize-pipeline.timer` | the 10-minute cadence. **This is what you enable.** |
| `organize-pipeline.path` | optional: also run within seconds of a capture landing. |
| `organize-pipeline-failure@.service` | the `OnFailure=` alerter. Templated, so `%i` is the unit that failed. Pulled in automatically; never enabled. |
| `organize-pipeline-alert.sh` | what the alerter runs: journal marker + desktop notification. |
| `organize-pipeline-failtest.service` | a deliberate failure, wired to the same alerter, so you can prove alerting works. |

### Why the alerter is a template (`@`)

`OnFailure=organize-pipeline-failure@%n.service` passes the *failed unit's
name* into the alert as `%i`. One alerter therefore covers the timer run,
the path-triggered run and the failtest — and every alert says which unit it
is about. A non-templated `organize-pipeline-failure.service` would have to
hardcode one unit name and would lie the moment a second unit used it.

## The failure this replaces

The live pipeline crashed on every run from 2026-05-10 to 2026-08-15 with a
`UnicodeDecodeError` (spec 08 §B1). Nobody noticed for three months, because
the chain was:

```
second-brain-automation.timer
  └─ second-brain-automation.service
       └─ ~/Obsidian/Main/scripts/second-brain-automation.py    # vault-side shim
            ├─ subprocess.run([...], check=True)  →  CalledProcessError
            │    └─ except: print("PARA Automation failed")     # ← swallowed
            └─ Beeper sync
       exit 0   →  systemd records SUCCESS
```

So the design rules here are non-negotiable:

- **no wrapper script around the pipeline** — `ExecStart=` invokes `organize`
  directly, and its exit code is the unit's exit code;
- **no `-` prefix** on `ExecStart=` (that prefix tells systemd to ignore a
  nonzero exit — the same bug in unit-file form);
- **no `|| true`** anywhere on the pipeline path;
- **`OnFailure=`** on every unit that can fail;
- **`TimeoutStartSec=`** so a hung run becomes a reported failure rather
  than a silent stall.

`tests/test_consumer_research_deploy.py` asserts each of those against the
files in this directory, so the swallow cannot be reintroduced quietly.

## Prerequisites

1. **`organize` on disk at a fixed path.** The units use
   `%h/.local/bin/organize`.

   ```sh
   # from a checkout of this repo
   python -m pip install --user .
   # or, from the development venv:
   ln -sf "$PWD/.venv/bin/organize" ~/.local/bin/organize
   ```

   Prefer a different location (a Nix profile, a venv elsewhere)? Do not
   edit the shipped unit — use a drop-in, which survives reinstalling these
   files:

   ```sh
   systemctl --user edit organize-pipeline.service
   ```
   ```ini
   [Service]
   ExecStart=
   ExecStart=/path/to/organize --log-level INFO --config %h/.config/organize-core/config.toml run-consumers
   ```

   (The empty `ExecStart=` is required — it clears the shipped value instead
   of appending to it.)

2. **A config file at `~/.config/organize-core/config.toml`.** The unit
   passes it explicitly, so there is never a question of which config a
   background run used (spec 08 §C4: the old repo and live `automations.toml`
   had drifted apart).

   ```sh
   mkdir -p ~/.config/organize-core
   organize health --example-config > ~/.config/organize-core/config.toml
   $EDITOR ~/.config/organize-core/config.toml
   organize health          # must be clean before you enable anything
   ```

   Hosts and models are config-required — there are no hardcoded fallbacks
   in code any more (spec 06 §2), so `[llm] ollama_host`/`ollama_model` must
   be set if you enable an LLM-using consumer. Nothing needs `OLLAMA_HOST`
   in the unit's environment; that was the old wrapper's job.

3. **`notify-send`** if you want desktop alerts (optional; the journal
   marker works without it).

4. **The external tools the consumers shell out to** — `task` (taskwarrior),
   `yt-dlp` (learn's YouTube path), and whatever `[consumers.deep_research]
   command` names — must resolve for the *systemd user manager*, whose `PATH`
   is not your interactive shell's. On NixOS in particular a binary that
   works in your terminal may be absent from the unit's environment.

   `ExecStart=` invokes `organize` directly with no wrapper and no
   `Environment=` (that is the point — see the design rules above), so pin
   the tools in the CONFIG rather than in the unit:

   ```toml
   [consumers.taskwarrior]
   task_binary = "/run/current-system/sw/bin/task"
   ```

   `organize health` resolves every external binary named by an enabled
   consumer and warns, by consumer and option name, about any that is
   missing. Run it before you enable the timer:

   ```sh
   organize health
   ```

   (Spec 06 §5 asked for a `shell.nix` here; it is deliberately retired in
   favour of the no-wrapper unit plus this check — see ARCHITECTURE.md.)

## Install

```sh
cd deploy
install -Dm755 organize-pipeline-alert.sh ~/.local/bin/organize-pipeline-alert
install -Dm644 -t ~/.config/systemd/user \
    organize-pipeline.service \
    organize-pipeline.timer \
    organize-pipeline.path \
    organize-pipeline-failure@.service \
    organize-pipeline-failtest.service
systemctl --user daemon-reload
```

> **STOP — retire the old chain first (spec 09 §5.5).** Do not enable the
> timer or the path unit until cutover step 5 below has removed the OLD
> chain's PARA step (`second-brain-automation` /
> `para-automation-watcher.path` / `para-automation.{service,timer}`). Both
> generations point at the same vault, so running them together produces
> duplicate Taskwarrior tasks, duplicate flashcards and duplicate answer
> notes, and doubles the LLM spend — and the second generation's writes are
> not idempotent against the first's. If you are only installing the units to
> read them, stop after `daemon-reload`.
>
> Mask, do not merely stop, anything you are retiring — a stopped unit is one
> `daemon-reload` or reboot away from coming back:
>
> ```sh
> systemctl --user mask para-automation.service para-automation.timer \
>     para-automation-watcher.path
> ```

Enable the cadence:

```sh
systemctl --user enable --now organize-pipeline.timer
systemctl --user list-timers organize-pipeline.timer
```

Optionally add the low-latency trigger (reacts to a capture landing instead
of waiting up to 10 minutes):

```sh
systemctl --user enable --now organize-pipeline.path
```

Do **not** `enable` `organize-pipeline.service` itself — it has no
`[Install]` section on purpose. It is started by the timer and the path
unit.

> **NixOS / home-manager:** `~/.config/systemd/user` here contains symlinks
> into the Nix store for home-manager-managed units plus plain files for
> hand-installed ones. These are hand-installed files; home-manager leaves
> them alone, but if you later move the pipeline into your home-manager
> config, delete these copies first so two definitions cannot disagree.

## Verify that failure is actually reported

This is step 5 of the cutover checklist and the single most important check
in this document. Do it **before** trusting the pipeline.

**1 — the alert path (systemd wiring):**

```sh
systemctl --user start organize-pipeline-failtest.service   # exits 17
journalctl --user -t organize-pipeline-alert -n 30 --no-pager
```

Expect a block naming `organize-pipeline-failtest.service`, its result and
exit status. With a desktop session you should also get a critical
notification. If the journal shows nothing, alerting is broken — fix it
before going further.

**2 — the CLI propagates nonzero (no swallow):**

```sh
organize --config /nonexistent/config.toml run-consumers; echo "exit=$?"
```

Expect a non-zero `exit=`. Zero here means something is swallowing failures
again.

**3 — a real run:**

```sh
systemctl --user start organize-pipeline.service
systemctl --user status organize-pipeline.service
journalctl --user -u organize-pipeline.service -n 50 --no-pager
```

Expect one `Consumer <name>: success=N skip=N limit=N error=N filtered=N`
summary line per consumer (spec 06 §4) and `Active: inactive (dead)` with
`status=0/SUCCESS`.

## Cutover from the old chain (spec 09 §5.5)

Do these in order. Steps 1–4 are other seats' deliverables and are listed so
the sequence is complete; steps 5–7 are this directory's.

1. Fix the live nvim config (`vault_dir = "~/notes"`,
   `para_folders.archives = "archive"`) and confirm
   `:checkhealth para-organize` is green.
2. Regenerate the plugin index from scratch; carry nothing over from the
   stale `index.json`.
3. Start `learning.json` fresh on the versioned schema.
4. **Back up, then migrate `automations.db`** (`success` history preserved,
   `filtered` rows dropped — spec 06 §1). Do not skip the migration: without
   it every past capture re-fires its consumers.

   **4a. Move the database to where this build reads it.** The old pipeline
   kept its state in `~/.local/state/para-organize/`; this build reads
   `<state-dir>/automations.db`, and `<state-dir>` defaults to
   `~/.local/share/organize-core` (spec 10 §3 — confirm with
   `organize health --json | jq -r .state_dir`). The shipped
   `organize-pipeline.service` passes no `--state-dir`, so it opens the
   DEFAULT path. Migrating the old file in place therefore migrates a
   database nothing will ever open, and the pipeline starts on an empty one —
   re-firing all 7 516 historical captures through taskwarrior, learn and
   question_answer. That is the outcome spec 06 §1 exists to prevent.

   Copy first, migrate the copy:

   ```sh
   mkdir -p ~/.local/share/organize-core
   cp ~/.local/state/para-organize/automations.db \
      ~/.local/share/organize-core/automations.db
   ```

   Leave the original where it is until the first real run looks right; it is
   your rollback.

   **4b. Rehearse, then migrate.** The CLI backs up and migrates, and it
   refuses to touch a database inside a live state directory unless you say
   `--yes-live` — so this is the one command in the repo you have to aim
   deliberately:

   ```sh
   cp ~/.local/share/organize-core/automations.db /tmp/automations-rehearsal.db
   organize migrate-store --db /tmp/automations-rehearsal.db --json
   ```

   ```sh
   organize migrate-store --db ~/.local/share/organize-core/automations.db --backup-first --yes-live
   ```

   Read the rehearsal report, then run the real one. `--backup-first` writes
   `<db>.backup-<UTC-ts>` next to the database before touching it.

   `organize health` fails with an ERROR if you skip 4a — it notices an old
   database holding emissions while the one this build reads is empty or
   missing, and prints the `cp` to run. Check it before enabling the timer:

   ```sh
   organize health
   ```

   Paste the report line into the cutover log. Measured on the real 15 MB
   database (2026-08-16): `v1->v2 notes=7516 success=376 skip=3 retryable=8
   filtered_dropped=22497 anomalies=0`, 15.3 MB → 5.3 MB, 0.25 s. A second
   run is a no-op and says so.

   With no `--db`, `organize migrate-store` targets
   `<state-dir>/automations.db` — the same file the service unit's
   `run-consumers` opens, which is why 4a puts the history there.

   `run-consumers` also migrates on start, and takes its own
   `<db>.backup-<UTC-ts>` before it does, so a forgotten step 4b is
   recoverable. `--dry-run` never migrates: a rehearsal that would need one
   refuses and points back here (09 §5.6).

5. **Stop the old chain's PARA step, keep Beeper sync.** The two are welded
   together inside one vault-side script today; they must be separated, not
   both disabled.

   ```sh
   systemctl --user list-timers second-brain-automation.timer   # see it first
   ```

   The PARA step is the *first* block of
   `~/Obsidian/Main/scripts/second-brain-automation.py`: it shells out to
   `.../organize/scripts/systemd/second-brain-automation.sh`, which runs the
   OLD `python -m scripts.automation.cli`. Delete (or comment out) that
   block only. Leave the Beeper sync block and leave
   `second-brain-automation.{service,timer}` enabled — Beeper is unrelated
   to this pipeline and must keep running (spec 06 §5: "do not couple it to
   this pipeline").

   Then retire the old capture watcher, which pointed at the same combined
   service:

   ```sh
   systemctl --user disable --now para-automation-watcher.path
   ```

   Verify the separation:

   ```sh
   systemctl --user start second-brain-automation.service
   journalctl --user -u second-brain-automation.service -n 30 --no-pager
   # expect: Beeper sync ran; no "Running PARA Automation..." line
   ```

   The repo-side `organize/scripts/systemd/*` units are already dead (their
   `ExecStart` names a file that does not exist — spec 08 §C3). Nothing to
   disable; leave them as history.

6. **First supervised run.** Before letting the timer write anything,
   rehearse over the real vault:

   ```sh
   organize --config ~/.config/organize-core/config.toml --dry-run run-consumers
   ```

   Read the summary; confirm the intended actions match expectations
   (spec 09 §5.6). Only then `enable --now` the timer.

7. **Watch it for 48 h** (spec 09 §6): new captures processed, correct
   summaries in the journal, no duplicate Taskwarrior tasks, and any failure
   visibly alerted.

   ```sh
   journalctl --user -u organize-pipeline.service --since '48 hours ago' | \
       grep -E 'Consumer |error='
   ```

## Operating it

```sh
# run once, now
systemctl --user start organize-pipeline.service

# only one consumer, in the foreground, verbosely
organize --log-level DEBUG --config ~/.config/organize-core/config.toml \
    run-consumers --consumer deep_research

# what is even registered (constructs nothing — spec 06 §4 / 08 §B2)
organize run-consumers --list-consumers

# every failure alert ever raised
journalctl --user -t organize-pipeline-alert --since '30 days ago'

# pause without forgetting state
systemctl --user stop organize-pipeline.timer
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Failed to locate executable %h/.local/bin/organize` | `organize` is not installed at that path — see Prerequisites 1, or add the drop-in. |
| Unit fails immediately, journal names a config key | The unknown-key law (spec 03 §1): fix or delete the key. `organize health` says which. |
| Runs, but every consumer reports `filtered=` only | `include_paths` do not match your vault layout. Filter misses are re-evaluated every run (spec 06 §1), so widening the paths applies retroactively — no state to clear. |
| Timer shows `n/a` for next run | The timer is not enabled: `systemctl --user enable --now organize-pipeline.timer`. |
| Alerts never arrive | Run the failtest above. If the journal marker appears but no notification does, the timer fired without a session bus — that is expected; the journal is the reliable channel. |
| An empty `~/notes/capture/raw_capture` appeared | `MakeDirectory=true` on the path unit created it while the vault was unmounted. Emission history is safe (spec 06 §1 refuses to purge on a missing/empty scan dir); mount the vault and the next run is normal. |
| Runs take longer than 15 minutes and get killed | `TimeoutStartSec=900`. Investigate the slow consumer first (a remote Ollama or research agent hanging); raise the value with a drop-in only if the wait is genuinely expected. |

## Validating changes to these files

```sh
cd deploy && systemd-analyze --user verify ./organize-pipeline.service
```

`systemd-analyze` reports `Command ... is not executable` unless `organize`
is actually installed at `%h/.local/bin/organize`; that message is about
your machine, not the unit. The automated check in
`tests/test_consumer_research_deploy.py` substitutes a real executable so it
validates the *directives* on any machine, and skips itself entirely where
`systemd-analyze` is unavailable.
