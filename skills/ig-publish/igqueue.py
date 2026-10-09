#!/usr/bin/env python3
"""
igqueue.py - a local publishing queue for Instagram, and nothing else.

This file keeps drafts in folders, checks them, and tells you what would be
published. It does not publish. There is no network code in here: no HTTP
client, no token handling, no uploads, no background jobs, no retries. The
only thing it ever writes is post.json inside the queue, plus the caption and
media files it copies in when you create a draft.

One folder per post:

  ~/.claude/instagram/queue/<id>/
      post.json      id, type, status, schedule, file names, history
      caption.txt    the caption, UTF-8
      <media file>   the video or image, copied in by `new`

Statuses: draft, validated, approved, published, failed, missed, cancelled.
This stage only moves a post between draft and validated. Approval is a
separate action that does not exist yet; when it does, it is bound to the
exact post id and to the SHA-256 of the caption and of every media file, and
`validate` already voids any approval whose content has changed since.

The media check is deliberately shallow: the file exists, is not empty, is
under the size limit and has an allowed extension. That says nothing about
the codec, the duration or the aspect ratio. Do not read a green check here
as "Instagram will accept this file".

Usage
  python3 igqueue.py new --id 2026-10-12-seva --type reel \\
      --when "2026-10-12 19:00" --tz Europe/Moscow \\
      --caption-file caption.txt --media ~/Movies/seva.mp4
  python3 igqueue.py check 2026-10-12-seva      # structure and fields
  python3 igqueue.py content 2026-10-12-seva    # caption and media
  python3 igqueue.py validate 2026-10-12-seva   # both, then draft -> validated
  python3 igqueue.py list
  python3 igqueue.py dry-run 2026-10-12-seva    # what would go out; no network
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = 1
DEFAULT_QUEUE = "~/.claude/instagram/queue"

STATUSES = ("draft", "validated", "approved", "published", "failed", "missed", "cancelled")
FINAL = ("published", "failed", "missed", "cancelled")   # validate never touches these

CAPTION_LIMIT = 2200     # characters
HASHTAG_LIMIT = 30
MENTION_LIMIT = 20

VIDEO_EXT = (".mp4", ".mov")
IMAGE_EXT = (".jpg", ".jpeg")
VIDEO_MAX = 300 * 1024 * 1024
IMAGE_MAX = 8 * 1024 * 1024

# type -> (allowed extensions, min files, max files, how it would be published)
TYPES = {
    "reel":     (VIDEO_EXT, 1, 1, "Instagram API, будущий этап"),
    "photo":    (IMAGE_EXT, 1, 1, "вручную через Meta Business Suite"),
    "carousel": (IMAGE_EXT + VIDEO_EXT, 2, 10, "вручную через Meta Business Suite"),
}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
MENTION_RE = re.compile(r"(?<!\w)@[\w.]+")
WHEN_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")
RESERVED = ("post.json", "caption.txt")


class QueueError(Exception):
    """A problem the user has to fix before the command can run at all."""


# --------------------------------------------------------------- small helpers

def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_root(arg=None):
    return Path(arg or os.environ.get("IG_QUEUE_DIR") or DEFAULT_QUEUE).expanduser()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def plain_name(name):
    """True if name is a bare file name that cannot point outside the post folder."""
    return (isinstance(name, str) and name not in ("", ".", "..")
            and "/" not in name and "\\" not in name and "\x00" not in name)


def human_size(n):
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024:
            return "%.0f %s" % (n, unit) if unit == "Б" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f ГБ" % n


def parse_when(value, tz_name):
    """Return (local_datetime, errors, warnings) for a naive local time in an IANA zone."""
    errors, warnings = [], []
    if not isinstance(value, str):
        return None, ["scheduled_at: нужна строка вида 2026-10-12T19:00"], warnings
    naive = None
    for fmt in WHEN_FORMATS:
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            pass
    if naive is None:
        return None, ["scheduled_at: «%s» не дата. Формат: 2026-10-12T19:00, без смещения "
                      "и без Z, часовой пояс задаётся полем timezone" % value], warnings
    try:
        tz = ZoneInfo(tz_name) if isinstance(tz_name, str) and tz_name else None
    except Exception:
        tz = None
    if tz is None:
        return None, ["timezone: «%s» не часовой пояс IANA. Пример: Europe/Moscow" % tz_name], warnings
    local = naive.replace(tzinfo=tz)
    # A time inside a clock-forward gap does not exist: it does not survive a round trip.
    if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        errors.append("scheduled_at: времени %s не существует в %s (перевод часов)" % (value, tz_name))
    elif local.utcoffset() != local.replace(fold=1).utcoffset():
        warnings.append("scheduled_at: время %s бывает в %s дважды (перевод часов), "
                        "берётся первое" % (value, tz_name))
    return local, errors, warnings


# ------------------------------------------------------------------ queue I/O

def post_dir(root, post_id):
    if not plain_name(post_id):
        raise QueueError("ID «%s» не подходит: это должно быть имя папки без слэшей" % post_id)
    d = root / post_id
    if not d.is_dir():
        raise QueueError("в очереди нет публикации «%s» (%s)" % (post_id, root))
    return d


def load_post(d):
    """Return (post_dict_or_None, error_or_None). Never raises on bad content."""
    f = d / "post.json"
    if not f.is_file():
        return None, "нет файла post.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, "post.json не читается как JSON: %s" % e
    if not isinstance(data, dict):
        return None, "post.json должен быть объектом"
    return data, None


def save_post(d, post):
    """Write post.json through a temp file so a crash cannot leave half a file."""
    post["updated_at"] = now_utc()
    tmp = d / "post.json.tmp"
    tmp.write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, d / "post.json")


def all_dirs(root):
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def read_caption(d, post):
    name = post.get("caption_file")
    if not plain_name(name):
        return None
    f = d / name
    if not f.is_file() or f.is_symlink():
        return None
    try:
        return f.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------- the checks

def check_structure(root, d, post):
    """Required fields, types, id, status, schedule. Returns (errors, warnings)."""
    errors, warnings = [], []

    if post.get("schema") != SCHEMA:
        errors.append("schema: ожидается %d" % SCHEMA)

    pid = post.get("id")
    if not isinstance(pid, str) or not ID_RE.match(pid):
        errors.append("id: 3-64 знака, только a-z, 0-9 и дефис, начинается с буквы или цифры")
    elif pid != d.name:
        errors.append("id: «%s» не совпадает с именем папки «%s»" % (pid, d.name))
    else:
        for other in all_dirs(root):
            if other == d:
                continue
            o, _ = load_post(other)
            same_name = other.name.lower() == d.name.lower()
            if same_name or (o and isinstance(o.get("id"), str) and o["id"].lower() == pid.lower()):
                errors.append("id: «%s» уже занят папкой «%s»" % (pid, other.name))

    ptype = post.get("type")
    if ptype not in TYPES:
        errors.append("type: нужно одно из %s" % ", ".join(TYPES))

    if post.get("status") not in STATUSES:
        errors.append("status: «%s» не статус. Допустимые: %s" % (post.get("status"), ", ".join(STATUSES)))

    if not plain_name(post.get("caption_file")):
        errors.append("caption_file: нужно имя файла внутри папки публикации")

    media = post.get("media")
    if not isinstance(media, list) or not all(isinstance(m, str) for m in media):
        errors.append("media: нужен список имён файлов")
    else:
        if len(set(m.lower() for m in media)) != len(media):
            errors.append("media: имена файлов повторяются")
        for m in media:
            if not plain_name(m) or m.lower() in RESERVED:
                errors.append("media: «%s» не подходит, нужно имя файла внутри папки публикации" % m)

    when, e, w = parse_when(post.get("scheduled_at"), post.get("timezone"))
    errors += e
    warnings += w
    if when is not None and not e and post.get("status") not in FINAL:
        if when <= datetime.now(timezone.utc):
            warnings.append("scheduled_at: время %s %s уже прошло" % (post["scheduled_at"], post["timezone"]))

    return errors, warnings


def check_caption(d, post):
    errors, warnings = [], []
    name = post.get("caption_file")
    if not plain_name(name):
        return errors, warnings          # already reported by check_structure
    f = d / name
    if not f.is_file():
        return ["подпись: нет файла %s" % name], warnings
    if f.is_symlink():
        return ["подпись: %s - ссылка, нужен обычный файл" % name], warnings
    try:
        text = f.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["подпись: %s не в кодировке UTF-8" % name], warnings

    if not text.strip():
        errors.append("подпись: пустая")
        return errors, warnings
    text = text.strip()
    if len(text) > CAPTION_LIMIT:
        errors.append("подпись: %d знаков, лимит %d" % (len(text), CAPTION_LIMIT))
    elif len(text.encode("utf-16-le")) // 2 > CAPTION_LIMIT:
        # Emoji may be counted as two units each. We do not know which way Instagram counts.
        warnings.append("подпись: %d знаков, но из-за эмодзи может считаться длиннее %d"
                        % (len(text), CAPTION_LIMIT))
    tags = HASHTAG_RE.findall(text)
    if len(tags) > HASHTAG_LIMIT:
        errors.append("подпись: %d хэштегов, лимит %d" % (len(tags), HASHTAG_LIMIT))
    mentions = MENTION_RE.findall(text)
    if len(mentions) > MENTION_LIMIT:
        errors.append("подпись: %d упоминаний, лимит %d" % (len(mentions), MENTION_LIMIT))
    if "{{" in text or "}}" in text:
        errors.append("подпись: остался незаполненный шаблон {{...}}")
    return errors, warnings


def check_media(d, post):
    errors, warnings = [], []
    media, ptype = post.get("media"), post.get("type")
    if ptype not in TYPES or not isinstance(media, list):
        return errors, warnings          # already reported by check_structure
    allowed, lo, hi, _ = TYPES[ptype]
    if not media:
        errors.append("медиа: файл не указан")
    elif not lo <= len(media) <= hi:
        want = str(lo) if lo == hi else "от %d до %d" % (lo, hi)
        errors.append("медиа: для %s нужно файлов: %s, указано %d" % (ptype, want, len(media)))
    for m in media:
        if not plain_name(m) or m.lower() in RESERVED:
            continue                     # already reported by check_structure
        f = d / m
        ext = f.suffix.lower()
        if ext not in allowed:
            errors.append("медиа: %s - расширение не подходит для %s (допустимо: %s)"
                          % (m, ptype, ", ".join(allowed)))
        if f.is_symlink():
            errors.append("медиа: %s - ссылка, нужен сам файл в папке публикации" % m)
        elif not f.is_file():
            errors.append("медиа: нет файла %s" % m)
        else:
            size = f.stat().st_size
            limit = VIDEO_MAX if ext in VIDEO_EXT else IMAGE_MAX
            if size == 0:
                errors.append("медиа: %s пустой" % m)
            elif ext in allowed and size > limit:
                errors.append("медиа: %s весит %s, лимит %s" % (m, human_size(size), human_size(limit)))
    return errors, warnings


def check_all(root, d, post):
    errors, warnings = check_structure(root, d, post)
    for fn in (check_caption, check_media):
        e, w = fn(d, post)
        errors += e
        warnings += w
    return errors, warnings


# ---------------------------------------------------- fingerprint and approval

def fingerprint(d, post):
    """Everything an approval is bound to. Only call on a post that passed check_all."""
    return {
        "id": post["id"],
        "type": post["type"],
        "scheduled_at": post["scheduled_at"],
        "timezone": post["timezone"],
        "caption_sha256": sha256_file(d / post["caption_file"]),
        "media": [{"file": m, "bytes": (d / m).stat().st_size, "sha256": sha256_file(d / m)}
                  for m in post["media"]],
    }


def approval_state(post, fp):
    """'none', 'valid' or 'stale'. fp is the current fingerprint, or None if the post is broken.

    An approval counts only if it names this exact post id and its stored
    fingerprint equals the current one. Anything else is stale.
    """
    a = post.get("approval")
    if not a:
        return "none"
    if not isinstance(a, dict) or fp is None:
        return "stale"
    if a.get("id") != post.get("id") or a.get("fingerprint") != fp:
        return "stale"
    return "valid"


# ------------------------------------------------------------------- commands

def report(title, errors, warnings):
    print(title)
    for e in errors:
        print("  ОШИБКА    %s" % e)
    for w in warnings:
        print("  ВНИМАНИЕ  %s" % w)
    if not errors and not warnings:
        print("  OK")
    elif not errors:
        print("  OK, с замечаниями")


def targets(root, args):
    if getattr(args, "all", False):
        return all_dirs(root)
    if not args.id:
        raise QueueError("укажите ID публикации или --all")
    return [post_dir(root, args.id)]


def cmd_new(root, args):
    pid, ptype = args.id, args.type
    if not ID_RE.match(pid):
        raise QueueError("ID «%s» не подходит: 3-64 знака, только a-z, 0-9 и дефис" % pid)
    for other in all_dirs(root):
        o, _ = load_post(other)
        if other.name.lower() == pid.lower() or (o and str(o.get("id", "")).lower() == pid.lower()):
            raise QueueError("ID «%s» уже занят папкой «%s». Существующая публикация не тронута" % (pid, other.name))

    when = args.when.strip().replace(" ", "T", 1)
    _, e, _ = parse_when(when, args.tz)
    if e:
        raise QueueError("; ".join(e))

    sources = [Path(m).expanduser() for m in (args.media or [])]
    for s in sources:
        if not s.is_file():
            raise QueueError("медиафайл не найден: %s" % s)
        if s.name.lower() in RESERVED:
            raise QueueError("медиафайл не может называться %s" % s.name)
    if len(set(s.name.lower() for s in sources)) != len(sources):
        raise QueueError("у медиафайлов одинаковые имена, переименуйте один из них")
    caption_src = Path(args.caption_file).expanduser() if args.caption_file else None
    if caption_src and not caption_src.is_file():
        raise QueueError("файл подписи не найден: %s" % caption_src)

    d = root / pid
    d.mkdir(parents=True)                 # fails if it exists; nothing is ever overwritten
    if caption_src:
        shutil.copyfile(caption_src, d / "caption.txt")
    else:
        (d / "caption.txt").write_text("", encoding="utf-8")
    for s in sources:
        shutil.copy2(s, d / s.name)       # a copy: the original stays where it was

    stamp = now_utc()
    post = {
        "schema": SCHEMA,
        "id": pid,
        "type": ptype,
        "status": "draft",
        "scheduled_at": when,
        "timezone": args.tz,
        "caption_file": "caption.txt",
        "media": [s.name for s in sources],
        "created_at": stamp,
        "validated": None,
        "approval": None,
        "history": [{"at": stamp, "event": "created"}],
    }
    save_post(d, post)
    print("Черновик создан: %s" % d)
    if not caption_src:
        print("  подпись пустая: впишите текст в %s" % (d / "caption.txt"))
    if not sources:
        print("  медиа нет: положите файл в папку и впишите его имя в поле media в post.json")
    print("  дальше: python3 igqueue.py validate %s" % pid)
    return 0


def run_checks(root, args, title, fn):
    code = 0
    dirs = targets(root, args)
    if not dirs:
        print("Очередь пуста: %s" % root)
    for d in dirs:
        post, err = load_post(d)
        if err:
            report("%s  %s" % (title, d.name), [err], [])
            code = 1
            continue
        errors, warnings = fn(d, post)
        report("%s  %s" % (title, d.name), errors, warnings)
        code = code or (1 if errors else 0)
    return code


def cmd_check(root, args):
    return run_checks(root, args, "СТРУКТУРА", lambda d, p: check_structure(root, d, p))


def cmd_content(root, args):
    def both(d, post):
        e1, w1 = check_caption(d, post)
        e2, w2 = check_media(d, post)
        return e1 + e2, w1 + w2
    code = run_checks(root, args, "ПОДПИСЬ И МЕДИА", both)
    print("Проверено только наличие, размер и расширение файлов. Кодек, длительность и "
          "пропорции не проверяются.")
    return code


def cmd_validate(root, args):
    code = 0
    dirs = targets(root, args)
    if not dirs:
        print("Очередь пуста: %s" % root)
    for d in dirs:
        post, err = load_post(d)
        if err:
            report("ПРОВЕРКА  %s" % d.name, [err], [])
            code = 1
            continue
        errors, warnings = check_all(root, d, post)
        report("ПРОВЕРКА  %s" % d.name, errors, warnings)
        code = code or (1 if errors else 0)

        status = post.get("status")
        if status in FINAL or status not in STATUSES:
            if status in FINAL:
                print("  статус %s конечный, не меняется" % status)
            continue

        fp = None if errors else fingerprint(d, post)
        before = json.dumps(post, sort_keys=True, ensure_ascii=False)
        stamp = now_utc()
        history = post.setdefault("history", [])

        if approval_state(post, fp) == "stale":
            post["approval"] = None
            history.append({"at": stamp, "event": "approval_voided",
                            "note": "содержимое изменилось после подтверждения"})
            print("  подтверждение аннулировано: публикация изменилась после него")
            status = "validated"

        if errors:
            new_status, post["validated"] = "draft", None
        elif status == "approved" and post.get("approval"):
            new_status = "approved"
        else:
            new_status = "validated"
            if (post.get("validated") or {}).get("fingerprint") != fp:
                post["validated"] = {"at": stamp, "fingerprint": fp}
        if new_status != post.get("status"):
            history.append({"at": stamp, "event": "status",
                            "from": post.get("status"), "to": new_status})
            post["status"] = new_status

        if json.dumps(post, sort_keys=True, ensure_ascii=False) != before:
            save_post(d, post)
        print("  статус: %s" % post["status"])
    return code


def effective(root, d, post):
    """Read-only view used by list and dry-run: (errors, warnings, fp, approval, stale_validation)."""
    errors, warnings = check_all(root, d, post)
    fp = None if errors else fingerprint(d, post)
    stale = (post.get("status") == "validated"
             and (post.get("validated") or {}).get("fingerprint") != fp)
    return errors, warnings, fp, approval_state(post, fp), stale


def cmd_list(root, args):
    dirs = all_dirs(root)
    if not dirs:
        print("Очередь пуста: %s" % root)
        return 0
    print("ОЧЕРЕДЬ  %s" % root)
    print("%-32s %-9s %-10s %-34s %s" % ("id", "тип", "статус", "когда", "замечания"))
    for d in dirs:
        post, err = load_post(d)
        if err:
            print("%-32s %-9s %-10s %-34s %s" % (d.name, "?", "?", "?", err))
            continue
        errors, _, _, approval, stale = effective(root, d, post)
        notes = []
        if errors:
            notes.append("ошибок: %d" % len(errors))
        if stale:
            notes.append("изменено после проверки")
        if approval == "stale":
            notes.append("подтверждение недействительно")
        when = "%s %s" % (post.get("scheduled_at"), post.get("timezone"))
        print("%-32s %-9s %-10s %-34s %s" % (d.name, post.get("type"), post.get("status"),
                                            when, "; ".join(notes) or "-"))
    return 0


def cmd_dry_run(root, args):
    """Show what would be published. Reads only: no file is written, no connection is opened."""
    d = post_dir(root, args.id)
    post, err = load_post(d)
    print("DRY-RUN  %s" % d.name)
    print("  сетевых действий: 0. Публикация на этом этапе не реализована.")
    if err:
        print("  ОШИБКА    %s" % err)
        print("ИТОГ: НЕ ГОТОВО")
        return 1
    errors, warnings, fp, approval, stale = effective(root, d, post)
    status, ptype = post.get("status"), post.get("type")

    print("  тип:            %s -> %s" % (ptype, TYPES[ptype][3] if ptype in TYPES else "?"))
    when, when_err, _ = parse_when(post.get("scheduled_at"), post.get("timezone"))
    if when is not None and not when_err:
        print("  когда:          %s %s (UTC %s)" % (when.strftime("%Y-%m-%d %H:%M"), post["timezone"],
                                                   when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")))
    print("  статус:         %s%s" % (status, " (изменено после проверки)" if stale else ""))
    caption = read_caption(d, post)
    if caption and caption.strip():
        text = caption.strip()
        first = text.splitlines()[0]
        print("  подпись:        %d из %d знаков, хэштегов %d" % (len(text), CAPTION_LIMIT,
                                                                  len(HASHTAG_RE.findall(text))))
        print("  первая строка:  %s" % (first if len(first) <= 90 else first[:87] + "..."))
        if fp:
            print("  хеш подписи:    %s" % fp["caption_sha256"][:16])
    for m in (fp or {}).get("media", []):
        print("  медиа:          %s, %s, хеш %s" % (m["file"], human_size(m["bytes"]), m["sha256"][:16]))
    print("  подтверждение:  %s" % {"none": "нет", "valid": "есть, действительно",
                                    "stale": "НЕДЕЙСТВИТЕЛЬНО, содержимое изменилось"}[approval])
    for e in errors:
        print("  ОШИБКА    %s" % e)
    for w in warnings:
        print("  ВНИМАНИЕ  %s" % w)

    if errors:
        print("ИТОГ: НЕ ГОТОВО. Исправьте ошибки и запустите validate.")
        return 1
    if status in FINAL:
        print("ИТОГ: статус %s, публикации не подлежит." % status)
    elif status == "draft" or stale:
        print("ИТОГ: проверки пройдены, но статус не зафиксирован. Запустите validate.")
    elif approval == "valid" and status == "approved":
        print("ИТОГ: ПОДТВЕРЖДЕНО. На этом этапе всё равно ничего не публикуется.")
    else:
        print("ИТОГ: ГОТОВО К ПОДТВЕРЖДЕНИЮ. Без подтверждения публикация была бы заблокирована.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        description="Локальная очередь публикаций Instagram. Только проверка, без сети и без публикации.")
    p.add_argument("--queue", help="папка очереди (по умолчанию %s или $IG_QUEUE_DIR)" % DEFAULT_QUEUE)
    sub = p.add_subparsers(dest="command", required=True)

    n = sub.add_parser("new", help="создать черновик")
    n.add_argument("--id", required=True, help="например 2026-10-12-seva-shepot")
    n.add_argument("--type", required=True, choices=sorted(TYPES))
    n.add_argument("--when", required=True, help='местное время: "2026-10-12 19:00"')
    n.add_argument("--tz", required=True, help="часовой пояс IANA, например Europe/Moscow")
    n.add_argument("--caption-file", help="файл с подписью, копируется в черновик")
    n.add_argument("--media", action="append", help="медиафайл, копируется в черновик; можно несколько раз")

    for name, text in (("check", "структура и обязательные поля"),
                       ("content", "подпись и формат медиа"),
                       ("validate", "все проверки, затем draft -> validated")):
        c = sub.add_parser(name, help=text)
        c.add_argument("id", nargs="?")
        c.add_argument("--all", action="store_true", help="вся очередь")

    sub.add_parser("list", help="показать очередь")
    dr = sub.add_parser("dry-run", help="показать, что было бы опубликовано; без сети")
    dr.add_argument("id")
    return p


COMMANDS = {"new": cmd_new, "check": cmd_check, "content": cmd_content,
            "validate": cmd_validate, "list": cmd_list, "dry-run": cmd_dry_run}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](queue_root(args.queue), args)
    except QueueError as e:
        print("ОШИБКА: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
