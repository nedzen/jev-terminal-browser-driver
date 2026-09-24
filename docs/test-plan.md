# Release test plan

Paste each prompt into a fresh Hermes `--tui` session with the `jev-driver`
plugin enabled and **Debug overlay** on. Run them in order; later prompts reuse
the tab from earlier ones.

After each prompt, check the pass line and the log:

```bash
tail -n 40 ~/.cache/jev-driver/drive.log
```

A run is one `run` line followed by `tick`/`act` lines and a final `done` or
`blocked`. `stale`, `retry_click`, `look_further`, and `continuity` lines
explain every recovery.

## 1. Open a page and stop

> Use jev_drive to open https://en.wikipedia.org/wiki/Main_Page and stop when the main page is showing.

Pass: one `jev_drive` call, `done`, no clicks.

## 2. Multi-step search in one call

> Search Wikipedia for "string trimmer" and open that article. Use a single jev_drive call; the goal is done when the String trimmer article is showing.

Pass: one call, actions like `Search → Search Wikipedia "string trimmer" → Search → DONE`,
final URL `/wiki/String_trimmer`. The panel lists each step with the typed text.

## 3. Read without clicking

> Using jev_read on the same tab, give me the section headings of this article and the first sentence under History.

Pass: `jev_read` only (no `jev_drive`), one outline call and at most one script
call, correct headings. No `script returned undefined`.

## 4. Target below the fold

> On the same article, click the "Husqvarna" link in the History section and stop when the Husqvarna article is open.

Pass: `done` on `/wiki/Husqvarna`. If the link starts off-screen the log shows
`look_further` scrolls before the click, not an early `blocked`.

## 5. Honest failure

> On the current page, click the "Buy now" button. Do not navigate anywhere else.

Pass: `blocked` after at most three `look_further` scrolls, and the agent reports
that there is no such button. It must not retry the same goal more than once.

## 6. Live feed: navigate by clicks (no account changes)

> Open https://x.com/home. Using jev_drive, open Explore from the left navigation, then open the first trending topic. Done when a search results page for that topic is showing.

Pass: one call, `done` on an `/search?q=` URL. Few or no `stale` lines; none end the run.

## 7. Live feed: collect with scrolling

> Using jev_read, list the author and first 100 characters of the first 15 posts on https://x.com/explore/tabs/for-you. Scroll as needed.

Pass: `jev_read` with `scrolls` > 0, 15 rows, no `jev_drive` "scroll down" loops.

## 8. One toggle, exactly once (changes your account)

Skip this one unless you are fine with a like on your account.

> Like the first post on https://x.com/home exactly once, then stop.

Pass: one click on `… Like`, then `done`, or `blocked` with `toggle_undo`.
It must never click `… Liked` afterwards. Unlike it by hand after the test.

## 9. Continuity without a URL

> Without passing a url, use jev_drive to scroll to the top of the current page and stop.

Pass: `continuity=re-attach` in the `run` line, same tab, no new tab opened.

## 10. Debug panel follows you

1. Collapse the Jev panel in the pane by clicking its header.
2. > Use jev_drive to open https://en.wikipedia.org/wiki/Lawn_mower and stop when it shows.

Pass: the panel stays collapsed on the new page. Expand it, run prompt 1 again,
and it stays expanded. Outlines: one green ring with an operation pill on the
chosen target, dashed rings with a rank badge on runner-ups.

## What to send back when something fails

- The prompt.
- The tool result the agent got (status, reason, why, actions).
- `tail -n 60 ~/.cache/jev-driver/drive.log` taken right after the run.
