---
name: ig-publish
description: >-
  Keep a local queue of Instagram posts that are ready to go out, check each
  one, and show a dry run of what would be published. Use when the user says
  "add this to the queue", "queue this reel", "is this post ready", "check the
  queue", "dry run", "what is scheduled", or has a finished video and a caption
  and wants them lined up for a date. It does not publish anything.
---

# ig-publish

The other skills in this pack write things. This one keeps the finished ones in
a row and checks them.

**It does not publish, and at this stage it cannot.** `igqueue.py` has no
network code in it: no HTTP client, no token, no upload, no scheduler, no
retry. Do not add any of that while working from this file, and do not publish
by some other route (a browser, another tool) because a post in the queue looks
ready. A ready post is a post the user can now decide about.

```bash
python3 igqueue.py new --id ID --type reel --when "2026-10-12 19:00" --tz Europe/Moscow \
    --caption-file caption.txt --media ~/Movies/clip.mp4
python3 igqueue.py check ID        # structure and required fields
python3 igqueue.py content ID      # caption and media
python3 igqueue.py validate ID     # both, then draft -> validated
python3 igqueue.py list
python3 igqueue.py dry-run ID      # what would go out; reads only
```

Every checking command also takes `--all` in place of an id.

## Where things live

The queue is outside the repository, in `~/.claude/instagram/queue/`, one
folder per post:

```
queue/2026-10-12-seva-shepot/
    post.json      id, type, status, schedule, file names, history
    caption.txt    the caption, UTF-8
    seva.mp4       the media, copied in by `new`
```

`new` copies the caption and the media in and leaves the originals alone. It
refuses an id that is already taken. Nothing in the queue is ever deleted or
overwritten by the script.

Do not write secrets into `post.json`, the caption, or anywhere else here.
There is nothing in this stage that needs one.

## Before you queue something

1. Read `~/.claude/instagram/voice.md` and `plan.md` so the id, the date and
   the type match what was planned. Read them; do not edit them.
2. The caption should already have been through `/ig-caption` and `/ig-human`.
   This skill checks the hard limits, not whether the caption is any good.
3. **Never make up media.** If the user has not given a real file, create the
   draft without `--media` and say it is waiting for one. Do not generate a
   placeholder image or video to make a check pass.
4. Ask for the timezone if it is not known. Do not guess it from the user's
   locale: the schedule is in the audience's time.

## Types

| type | files | how it goes out |
| --- | --- | --- |
| `reel` | one `.mp4` or `.mov` | the Instagram API, in a later stage |
| `photo` | one `.jpg` or `.jpeg` | by hand in Meta Business Suite |
| `carousel` | 2 to 10 `.jpg`, `.jpeg`, `.mp4`, `.mov` | by hand in Meta Business Suite |

Photos and carousels sit in the queue so the week is in one place. The user
posts them.

## Statuses

`draft`, `validated`, `approved`, `published`, `failed`, `missed`, `cancelled`.

Only two transitions exist right now, and `validate` makes both:

- `draft` to `validated` when every check passes. The SHA-256 of the caption
  and of each media file is stored with it.
- back to `draft` when a check fails.

A `validated` post whose files changed afterwards shows as "changed since the
check" in `list` and `dry-run` until `validate` runs again.

The last four statuses are recognised and left untouched. Nothing sets them yet.

## Approval is a separate act, and it is the user's

There is no approve command. When one exists it will be bound to the exact
post id and to the hashes above, and the check for that is already in place:
`validate` voids any approval whose id or hashes no longer match and drops the
post back to `validated`, and `dry-run` reports it as invalid.

So:

- Never write an `approval` block into `post.json` yourself.
- Never set `status` to `approved` or `published` by hand.
- "Looks good" about a caption is not approval to publish. Approval is the
  user saying yes to one specific post, by id, after seeing its dry run.

## What the checks do and do not cover

Checked: the folder and `post.json` are well formed; the id is valid, matches
the folder and is unique; the date is real, in `YYYY-MM-DDTHH:MM` form, and
exists in the given IANA timezone; the caption is not empty, is at most 2,200
characters, has at most 30 hashtags and 20 mentions, and has no `{{...}}` left
in it; each media file is present, not empty, under the size limit, and has an
allowed extension.

**Not checked: what is inside the media file.** The extension says nothing
about the codec, the duration, the resolution or the aspect ratio. Say this to
the user whenever you report that a post passed. A passed check means "nothing
is obviously missing", not "Instagram will take this file".

## Reporting

Show the user the `dry-run` output for the post, not a summary of it. State the
status, whether an approval exists, and that nothing was sent anywhere. If a
check failed, say which one and what to change.
