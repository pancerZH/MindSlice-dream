# MindSlice-dream

[中文](README-zh.md)

A nightly "dream" for a MindSlice workspace (default `~/mindslice`). The script first checks whether there is anything to do tonight; if so, it starts one agy session that checks the memory cards against git, PyPI, CI and tests, corrects them, and tidies their structure. The session's `mindslice` is guarded by the program (in stage mode it stages every write; it refuses deletes, apply and the like), and when the session ends, the script checks that it stayed within bounds, then emails you the staged changes for review; reply to the email to approve, discard or amend them. See [PLAN.md](PLAN.md) for the design and decisions.

## Install

```bash
uv sync                      # the only dependency is mindslice>=0.2.2 (op=patch, --also, multi-address read); the CLI is installed in .venv
cp config.example.toml config.toml   # then edit workspace, watch.repos, etc.; config.toml stays on this machine and is not committed
uv run pytest                # fake harness: no real LLM calls, never touches ~/mindslice
```

## Run

```bash
uv run mindslice-dream run --dry-run   # run only the gate and print input.json; no agy session, state unchanged
uv run mindslice-dream run             # skips if the gate finds nothing to do
uv run mindslice-dream run --force     # starts a session whatever the gate says
uv run mindslice-dream --config other.toml run   # use another config (e.g. one pointing at a workspace copy)
```

Configuration lives in the local `config.toml` (template: `config.example.toml`): the workspace, the write mode (default `stage`: every write is staged for review via email, `mindslice diff` or `/mindslice:review`), the repositories to watch, the address review emails go to, and the agy command and timeout.

Each run's output goes to `runs/<YYYYMMDD-HHMMSS>/`: `input.json`, `prompt.md`, `stdout.log` / `stderr.log`, `report.md` (the dream's report, followed by the script's checks) and `meta.json` (status, trigger reasons, problems, resume command). `runs/latest.md` points to the latest report; `state.json` holds the checkpoint, which only advances when the checks pass.

## Try it on a copy first

```bash
uv run mindslice -w ~/mindslice backup /tmp/ms-backup            # back up ~/mindslice
uv run mindslice restore /tmp/ms-backup --output /tmp/ms-copy     # restore it as a new workspace
mkdir -p /tmp/dream-e2e && sed 's#~/mindslice#/tmp/ms-copy#' config.toml > /tmp/dream-e2e/config.toml
uv run mindslice-dream --config /tmp/dream-e2e/config.toml run --force
uv run mindslice -w /tmp/ms-copy diff                             # stage mode: see the staged changes
uv run mindslice -w ~/mindslice history --limit 10               # no [dream] writes should appear in the real workspace
```

`state.json` and `runs/` are written next to that config (`/tmp/dream-e2e`), so they don't mix with the real runs' state. This config keeps `[mail] to`, so changes on the copy also send a review email; to avoid that, set `to` to `""` in the copy's config.

## Email review

Set `[mail] to` in `config.toml` to your own address (e.g. a plus address of the sending account, `<account>+dream@gmail.com`) to turn it on. Sending and receiving use the Gmail account file that `account` points to (news-agent's `smtp.toml`; only its host, port, username and password are used).

- After the nightly run, if staging holds dream changes or the run did not succeed, one email is sent: each change has a number, its reason and its diff, followed by the run report. It is not resent if the pending changes are exactly the same as in the previous email.
- Reply to the email directly, giving numbers and what to do, e.g. "approve all", "approve 1-5, discard 6", "3: change X to Y, then approve". Anything not mentioned stays pending.
- A timer fetches replies every 10 minutes: an agy session turns the reply into a decision for each number, the program carries them out (an amendment patches the staged version first, then applies it), and a confirmation is sent in the same thread, with the final text of any amended card.
- Only replies that quote the review email, were sent from this account (Gmail's "Sent" label), and have the `to` address as both sender and recipient are accepted. Changes that have changed since the email was sent are not acted on.

```bash
uv run mindslice-dream mail          # fetch replies once and act on them (what the timer runs; exits at once if nothing is pending)
uv run mindslice-dream mail --send   # send a review email for the dream's staged changes right now
```

Each processed reply leaves `runs/<time>-review/`: `input.json` (the reply text and the changes), `prompt.md`, the two logs, `report.md` (the confirmation email's body) and `meta.json` (decisions and results).

## Scheduling

```bash
mkdir -p ~/.config/systemd/user
sed "s#@REPO@#$PWD#" systemd/mindslice-dream.service > ~/.config/systemd/user/mindslice-dream.service
cp systemd/mindslice-dream.timer ~/.config/systemd/user/
sed "s#@REPO@#$PWD#" systemd/mindslice-dream-mail.service > ~/.config/systemd/user/mindslice-dream-mail.service
cp systemd/mindslice-dream-mail.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mindslice-dream.timer        # daily at 03:00; a missed run catches up after boot
systemctl --user enable --now mindslice-dream-mail.timer   # fetch review replies every 10 minutes
journalctl --user -u mindslice-dream -u mindslice-dream-mail   # view output
```
