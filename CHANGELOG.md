# Changelog

Multiple Claude Code sessions work this repo independently and push straight
to `main` without talking to each other first — this file exists so a fresh
session (or a human) can see what recently shipped and why, without replaying
`git log`. If you're an agent picking this repo up mid-flight, start here.

**The convention:** `src/web/app.py` carries one `APP_VERSION` string
(`grep 'APP_VERSION = ' src/web/app.py`), surfaced in the UI's version badge
and `/health`. It gets bumped on every meaningful change — in practice, close
to 1:1 with commits. **When you bump it, add a row below** (newest on top) —
see `CLAUDE.md` for the exact instruction. One line is enough; the commit
itself carries the full "why."

Full commit history: `git log --oneline`. Commits that self-tag a version in
the subject: `git log --oneline --grep='(v0\.' -i`.

| Version | Date | Summary | Commit |
|---|---|---|---|
| 1.17 | 2026-11-04 | The burst re-aims at times polling actually sees, instead of re-firing a wrong floor for 5s | `b48e71f` |
| 1.16 | 2026-11-04 | Failed holds record the slot as advertised vs the payload sent, and name the mismatch | `eb227e4` |
| 1.15 | 2026-11-04 | A compromised race flags itself in its own summary (`races.warnings`) instead of waiting to be excavated | `e179184` |
| 1.14 | 2026-11-04 | A failed grid read no longer reads as "never seen" — retry, and never infer a grid or floor from a failed lookup | `a40c431` |
| 1.13 | 2026-10-25 | A weekday with no good release read borrows its floor from the course's same-type days | `422b1d9` |
| 1.12 | 2026-10-22 | HTTP/2 connection made safe to share between burst threads (10-17 / 10-21 failures); HTTP stack pinned; /health reports h2_guard | `671b6dd` |
| 1.11 | 2026-10-18 | Future Sight aims from the earliest time seen bookable at a release, not the window start | `a1660d1` |
| 1.10 | 2026-10-11 | Disagreeing grids split Future Sight's targets; no fake grids from two intervals meeting; harvest workflow exit fix | `dc1fbb7` |
| 1.09 | 2026-10-11 | Log whether the CPU quota throttles us during a race (`timing.cpu_throttle`); logging only | `7d86abc` |
| 1.08 | 2026-10-11 | A Future Sight hold on a fee course now ends in a payment link (fee recovery was poll-path only); payment hold keepalive actually runs | `adad13f` |
| 1.07 | 2026-09-20 | Take a hold won mid-poll before the poll path submits against it | `7a58f50` |
| 1.06 | 2026-09-01 | Infer an unseen weekday's grid from the same course's other days | `3a9652c` |
| 1.05 | 2026-09-01 | Today's shrinking teesheet can no longer thin a stored grid | `6aa4da1` |
| 1.04 | 2026-09-01 | A thin teesheet read can no longer overwrite a better stored grid | `9e64c45` |
| 1.03 | 2026-09-01 | Grid harvester learns guest-bookable courses too (a rejected login isn't always a wrong password) | `bce1ec8` |
| 1.02 | 2026-09-01 | "Refresh Tee-Grid Coverage" button in the UI, using the connected ForeUp account | `522c441` |
| 1.01 | 2026-09-01 | Bank the Future Sight arming decision on the race row; sink survives schema drift | `618037d` |
| 1.00 | 2026-09-01 | Fire pre-staged holds in parallel, alongside polling | `6203e3b` |
| 0.99 | 2026-08-31 | Recover into the payment flow whenever ForeUp asks for a fee | `68613a1` |
| 0.98 | 2026-08-31 | Fix the page structure broken by moving the prompts | `a7b9838` |
| 0.97 | 2026-08-31 | Fire pre-staged holds under the play date's booking class | `70e327c` |
| 0.96 | 2026-08-31 | Put the booking-fee prompt in front of the user, not in the log | `f9b624c` |
| 0.95 | 2026-08-31 | Hand back a payment link when the course charges a booking fee | `ae892e4` |
| 0.94 | 2026-08-31 | Correct the validate verdict: it is the normal step, not an ambiguous one | `8d1e8b2` |
| 0.93 | 2026-08-31 | Ask ForeUp for the hosted payment page, and hand back the URL | `a405e99` |
| 0.92 | 2026-08-31 | Follow minified references back to each checkout endpoint | `6ef015b` |
| 0.91 | 2026-08-31 | Recover the checkout API endpoints from ForeUp's bundle | `5182fef` |
| 0.90 | 2026-08-31 | Read ForeUp's checkout script to find the fields that pay the fee | `3b20112` |
| 0.89 | 2026-08-31 | Let the validate probe get past the 2FA gate, and stop refusing on-grid times | `1463b46` |
| 0.88 | 2026-08-31 | Ask ForeUp whether a booking would be accepted, without booking it | `86a8ef3` |
| 0.87 | 2026-08-31 | Stop scoring a booking-fee refusal as a lost race | `c61df51` |
| 0.86 | 2026-08-30 | Never aim Future Sight at hours the reference sheet did not cover | `4bce76f` |
| 0.85 | 2026-08-30 | Replace the course dropdown with a crest list | `806f2de` |
| 0.84 | 2026-08-30 | Course crests on the picker, un-shadow the restyled checkbox rows | `7665b24` |
| 0.83 | 2026-08-30 | Take the cold-hold probe out of the booking flow | `0ed1ae0` |
| 0.82 | 2026-08-30 | FAQ: document Future Sight, and correct what had drifted | `5ebdbc5` |
| 0.81 | 2026-08-30 | Make the tee-grid registry teachable; create the table it writes to | `9c0ae18` |
| 0.80 | 2026-08-30 | Stop our own users from racing each other; fix the telemetry schema | `c7ae773` |
| 0.78 | 2026-08-30 | Pick a course instead of pasting a ForeUp URL, and precheck the links | `456614b` |
| 0.78 | 2026-08-30 | Golf-club visual identity for the web UI | `e578587` |
| 0.77 | 2026-08-30 | Future Sight requires a release time; disable "Start now" | `60ee125` |
| 0.76 | 2026-08-30 | One strategy picker instead of two stacked checkboxes | `c027a9b` |
| 0.75 | 2026-08-30 | Future Sight becomes a toggle: targets derive from the user's window | `7a8bfa3` |
| 0.74 | 2026-08-30 | Future Sight: same-weekday reference, persistent grid registry, burst re-fire | `78c17da` |
| 0.73 | 2026-08-30 | Future Sight: pre-staged holds fired at the release instant | `b75969d` |
| 0.72 | 2026-08-30 | Actually collect the per-request telemetry, stop timing a fake path | `d39461a` |
| 0.71 | 2026-08-30 | Read the selected courses in the probe, not just the one in the URL | `0b6c25d` |
| 0.70 | 2026-08-30 | Add the cold-hold probe: can we hold a tee time without polling first? | `65b6622` |
| 0.69 | 2026-08-30 | Stop the false "SLOT FOUND" alert, skip re-warm when armed late | `a1c9775` |
| 0.68 | 2026-08-29 | Record whether the reservation hold survives the 2FA wait | `37584a3` |
| 0.67 | 2026-08-29 | Stop reporting a booking as confirmed on an unverified 200 | `8f7abe2` |
| 0.66 | 2026-08-28 | Stamp asset URLs with the version so caches cannot lie | `d4ea902` |
| 0.65 | 2026-08-28 | Stop snipe-now runs recording a phantom late start | `c43edb5` |
| 0.64 | 2026-08-28 | Add a stop button for running jobs | `472d044` |
| 0.62 | 2026-08-28 | Fix sniper late-start bug + add race loss diagnostics | `6a920ed` |
| 0.61 | 2026-05-11 | Sniper round-robin polling across selected courses | `9702aff` |
| 0.60 | 2026-05-11 | Sniper retry loop — keep racing the full 90s after first failure | `857ebde` |
| 0.59 | 2026-05-11 | Remove redundant sniper re-login + enable HTTP/2 | `4ea0505` |
| 0.57 | 2026-05-11 | Reorder Sniper Mode controls, add per-option descriptions | `f7f65fb` |
| 0.56 | 2026-05-11 | Add Supported Courses card to Step 1 | `6d2b56d` |
| 0.55 | 2026-05-11 | Move Dry Run into its own card, default-uncheck it | `b3350bd` |
| 0.54 | 2026-05-09 | Add 'Snipe Now' mode — poll immediately, no release timer | `81239ef` |
| 0.53 | 2026-05-09 | Anchor sniper timer to America/New_York | `e9e0888` |
| 0.52 | 2026-05-08 | Audit fixes: speed wins on the hot path + 5 correctness bugs | `34455df` |
| 0.51 | 2026-05-07 | TTID refresh + stale-slot retry loop | `72786e6` |
| 0.50 | 2026-05-07 | Drop 2FA/CAPTCHA caveats from auto-book label and FAQ | `cb23953` |
| 0.49 | 2026-05-07 | Fix duplicate reservation_confirmation_uid key clobbering the code | `f5de07f` |
| 0.48 | 2026-05-07 | Reactive 2FA recovery — trigger request-code flow on the 400 error | `3a29e5c` |
| 0.47 | 2026-05-07 | Implement Bethpage email-2FA booking-code flow | `d3c153a` |
| 0.46 | 2026-05-07 | Detect 'booking code required' error, emit clear 2FA terminal event | `06c51ae` |
| 0.45 | 2026-05-07 | Per-course booking class resolution for multi-course facilities | `54b5ac1` |
| 0.44 | 2026-05-07 | Book the exact dry-run slot instead of re-discovering | `e29af1c` |
| 0.43 | 2026-05-07 | Show course on results, add 'Book now' from dry run | `574a33d` |
| 0.42 | 2026-05-07 | Use 'title' field for course names | `eb8c59c` |
| 0.41 | 2026-05-07 | Multi-course discovery + ranking strategies (Bethpage-style facilities) | `b9ad97d` |
| 0.40 | 2026-05-07 | Implement full ForeUp 2-step booking flow + captcha for regular worker | `8bb96bd` |
| 0.36 | 2026-05-07 | Forward full ForeUp pricing snapshot in reservation payload | `86ebd77` |
| 0.35 | 2026-05-07 | Surface slot constraint fields in checkout debug | `67b595e` |
| 0.33 | 2026-05-07 | Add booking debug callback to surface payload and response in UI | `8400963` |

Earlier history predates version tracking; see `git log --oneline` for the
full record.

## Notable gaps this history surfaced

Worth knowing even though they're not their own version row:

- **`course_grids` table never existed** (referenced by `src/telemetry/grid_store.py`
  since it was written, created only at v0.81 / `9c0ae18`) — every grid-learning
  write silently failed until then. Two independent sessions found and fixed
  this within the same day (v0.81, and again while merging a parallel branch);
  `scripts/check_schema_drift.py` (added alongside) now guards against a repeat.
- **Multiple sessions can push to `main` in the same hour with zero coordination
  beyond a version bump in the commit.** If you're an agent: check `git log`
  before assuming your version number or a table/endpoint you're about to add
  doesn't already exist.
