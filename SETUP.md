# Setup

## 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the prompts.
2. BotFather gives you a token like `123456:ABC-DEF...` — save it.
3. Send any message (e.g. "hi") to your new bot from your own Telegram account, so it's allowed to message you back.
4. Get your chat ID:
   ```
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
   ```
   Look for `"chat":{"id":...}` in the response — that number is your `TELEGRAM_CHAT_ID`.

## 2. Push this repo to GitHub

```
gh auth login          # if not already logged in
gh repo create carnival-watch --public --source=. --remote=origin --push
```

(Public is fine and free — there are no secrets in the code, only in GitHub Secrets below. Public repos also get unlimited free Actions minutes, private repos are capped.)

## 3. Add repo secrets

Settings → Secrets and variables → Actions → New repository secret:

- `TELEGRAM_BOT_TOKEN` — from step 1
- `TELEGRAM_CHAT_ID` — from step 1

(`GITHUB_TOKEN` is automatic, no setup needed.)

## 4. Test it

Actions tab → "Watch Carnival Now Showing" → Run workflow (this is the
`workflow_dispatch` trigger). Watch the run logs — it should fetch the
current Now Showing list and log that Jananayagan wasn't found, then keep
polling. To confirm the Telegram side works end-to-end, temporarily set
`TARGET_MOVIE` to something currently showing (e.g. `Varavu`) and
`TARGET_DATE_VALUE` to today's date (e.g. `2026-07-19T00:00:00`) in the
workflow env, re-run, confirm you get both Telegram messages (movie found,
then showtime found), then change both back before the real window. If you
want to test the "listed but not yet bookable" path too, delete `state.json`
between test runs so it starts fresh from stage `movie` each time.

## 5. Let it run

Once secrets are set, the schedule in `.github/workflows/watch.yml` runs
automatically every 5 hours, each run polling every ~90 seconds internally.
Progress is tracked in `state.json`, committed back to the repo after each
run so restarts don't lose track or send duplicate alerts. You'll get a
Telegram message at each stage:

1. **Movie appears** — Jananayagan shows up in the Now Showing list.
2. **Matching showtime appears** — a showtime after 7:00 PM on 23 Jul shows
   up on the booking page, whether or not it's bookable yet (covers the case
   where an earlier show, e.g. 1:30 PM, is released first — you'll still get
   pinged the moment the one you actually want shows up).
3. **Now bookable** — if step 2's showtime wasn't bookable yet, a final alert
   fires the moment it flips to bookable. (If it was already bookable in
   step 2, this is skipped — you already got told to go book.)

After the final "bookable" alert, the workflow disables its own schedule.

If it fails 20 polls in a row (site/auth changed, network issue), you'll get
a Telegram warning so you know to check on it manually instead of silently
missing the launch.

## Re-arming for a future movie

To reuse this for the next movie: delete `state.json` (or reset its
`stage` back to `"movie"`), update `TARGET_MOVIE` / `TARGET_DATE_VALUE` /
`CUTOFF_HOUR` in `.github/workflows/watch.yml`, and re-enable the workflow
under Settings → Actions (it disables itself after firing).
