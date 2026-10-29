#!/usr/bin/env python3
"""recall: opt-in, local, long-term search over your Claude Code session history.

Claude Code writes every session to ~/.claude/projects/<project>/<session>.jsonl and may
delete old ones. recall copies the useful part (your prompts, the assistant's text replies,
session titles, tool names and file paths) into its own SQLite database, with secrets
redacted, so you can search it later. Nothing here talks to a network, and nothing is shown to
Claude unless you run `show` and hand the output over yourself.

Commands:
  sync      copy new/changed sessions into the archive (idempotent)
  search    full-text search; numbers the hits so `show` can load one
  show      print a passage (or a session) from the archive, capped in size
  list      most recent sessions in the archive
  stats     what the archive holds
  forget    delete a session from the archive and keep it from being re-imported
  install-job / uninstall-job   daily background sync on macOS (launchd)

Environment: RECALL_DB (default ~/.claude-history/history.db),
             RECALL_SOURCE (default ~/.claude/projects)
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
DB_PATH = Path(os.environ.get("RECALL_DB", str(HOME / ".claude-history" / "history.db")))
SOURCE = Path(os.environ.get("RECALL_SOURCE", str(HOME / ".claude" / "projects")))
LAST_SEARCH = DB_PATH.parent / "last_search.json"
JOB_LABEL = "local.claude-recall.sync"
DEFAULT_MAX_CHARS = 30000
MAX_TOOLS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  key TEXT PRIMARY KEY,
  session_id TEXT,
  project TEXT,
  git_branch TEXT,
  title TEXT,
  started_at TEXT,
  ended_at TEXT,
  is_subagent INTEGER NOT NULL DEFAULT 0,
  source_path TEXT,
  source_size INTEGER,
  source_mtime REAL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  session_key TEXT NOT NULL,
  seq INTEGER NOT NULL,
  uuid TEXT,
  role TEXT NOT NULL,
  ts TEXT,
  text TEXT NOT NULL,
  tools TEXT
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_key, seq);
CREATE TABLE IF NOT EXISTS forgotten(key TEXT PRIMARY KEY, forgotten_at TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

# Pattern-based, so it will not catch everything.
REDACTIONS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
]
ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|passwd|password)[A-Z0-9_]*)(\s*[:=]\s*)(['\"]?)([^\s'\"]{6,})"
)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def redact(text):
    for pattern in REDACTIONS:
        text = pattern.sub("[REDACTED]", text)
    return ASSIGNMENT.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "[REDACTED]", text)


# ---------------------------------------------------------------- database

class Archive(sqlite3.Connection):
    tokenizer = "trigram"


def connect():
    os.umask(0o077)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(str(DB_PATH.parent), 0o700)
    new = not DB_PATH.exists()
    db = sqlite3.connect(str(DB_PATH), factory=Archive)
    db.row_factory = sqlite3.Row
    if new:
        os.chmod(str(DB_PATH), 0o600)
    db.executescript(SCHEMA)
    row = db.execute("SELECT v FROM meta WHERE k='tokenizer'").fetchone()
    if row is None:
        tokenizer = "trigram"
        try:
            db.execute(
                "CREATE VIRTUAL TABLE messages_fts USING fts5("
                "text, content='messages', content_rowid='id', tokenize='trigram')"
            )
        except sqlite3.OperationalError:
            tokenizer = "unicode61"
            db.execute(
                "CREATE VIRTUAL TABLE messages_fts USING fts5("
                "text, content='messages', content_rowid='id', tokenize='unicode61')"
            )
        db.execute("INSERT INTO meta(k, v) VALUES ('tokenizer', ?)", (tokenizer,))
        db.executescript(FTS_TRIGGERS)
        db.commit()
    else:
        tokenizer = row["v"]
    db.tokenizer = tokenizer
    return db


# ---------------------------------------------------------------- sync

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def tool_labels(content):
    labels = []
    if not isinstance(content, list):
        return labels
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        name = block.get("name", "?")
        args = block.get("input") or {}
        path = args.get("file_path") or args.get("notebook_path") or args.get("path")
        labels.append("%s %s" % (name, path) if isinstance(path, str) else name)
    return labels


def parse_session(path, is_subagent):
    """Return (meta, messages) from one transcript file. Tolerates a half-written last line."""
    meta = {"session_id": None, "project": None, "git_branch": None, "title": None}
    messages = []
    pending_tools = []
    with open(str(path), encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            kind = obj.get("type")
            if kind == "ai-title" and obj.get("aiTitle"):
                meta["title"] = obj["aiTitle"]
                continue
            if kind not in ("user", "assistant"):
                continue
            if obj.get("isSidechain") and not is_subagent:
                continue
            if obj.get("isMeta"):
                continue
            meta["session_id"] = meta["session_id"] or obj.get("sessionId")
            meta["project"] = meta["project"] or obj.get("cwd")
            meta["git_branch"] = meta["git_branch"] or obj.get("gitBranch")
            content = (obj.get("message") or {}).get("content")
            text = SYSTEM_REMINDER.sub("", text_of(content)).strip()
            if kind == "assistant":
                pending_tools.extend(tool_labels(content))
            if not text:
                continue
            tools = None
            if kind == "assistant" and pending_tools:
                tools = "; ".join(list(dict.fromkeys(pending_tools))[:MAX_TOOLS])
                pending_tools = []
            messages.append({
                "uuid": obj.get("uuid"),
                "role": kind,
                "ts": obj.get("timestamp"),
                "text": redact(text),
                "tools": tools,
            })
    if pending_tools:
        for message in reversed(messages):
            if message["role"] == "assistant":
                joined = list(dict.fromkeys(
                    ([message["tools"]] if message["tools"] else []) + pending_tools))
                message["tools"] = "; ".join(joined[:MAX_TOOLS])
                break
    return meta, messages


def iter_sources(include_subagents):
    if not SOURCE.is_dir():
        return
    for project_dir in sorted(SOURCE.iterdir()):
        if not project_dir.is_dir():
            continue
        for path in sorted(project_dir.glob("*.jsonl")):
            yield path.stem, path, False
        if include_subagents:
            for path in sorted(project_dir.glob("*/subagents/*.jsonl")):
                yield "%s/%s" % (path.parent.parent.name, path.stem), path, True


def sync(db, include_subagents=False, quiet=False):
    counts = {"new": 0, "updated": 0, "unchanged": 0, "empty": 0, "shrunk": 0, "forgotten": 0}
    forgotten = set(r["key"] for r in db.execute("SELECT key FROM forgotten"))
    for key, path, is_subagent in iter_sources(include_subagents):
        if key in forgotten:
            counts["forgotten"] += 1
            continue
        stat = path.stat()
        row = db.execute(
            "SELECT source_path, source_size, source_mtime FROM sessions WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            if row["source_path"] != str(path):
                continue  # same session id from a different file: keep the first
            if row["source_size"] == stat.st_size and row["source_mtime"] == stat.st_mtime:
                counts["unchanged"] += 1
                continue
            if stat.st_size < row["source_size"]:
                counts["shrunk"] += 1  # never replace the archive with less data
                continue
        meta, messages = parse_session(path, is_subagent)
        if not messages:
            counts["empty"] += 1
            continue
        stamps = sorted(m["ts"] for m in messages if m["ts"])
        started, ended = (stamps[0], stamps[-1]) if stamps else (None, None)
        title = meta["title"] or messages[0]["text"].strip().splitlines()[0][:80]
        with db:
            db.execute("DELETE FROM messages WHERE session_key=?", (key,))
            db.execute(
                "INSERT OR REPLACE INTO sessions(key, session_id, project, git_branch, title,"
                " started_at, ended_at, is_subagent, source_path, source_size, source_mtime)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, meta["session_id"], meta["project"], meta["git_branch"], title,
                 started, ended, int(is_subagent), str(path), stat.st_size, stat.st_mtime),
            )
            # seq 0 is the title, so searches can match it
            db.execute(
                "INSERT INTO messages(session_key, seq, uuid, role, ts, text)"
                " VALUES (?,?,?,?,?,?)", (key, 0, None, "title", started, title))
            db.executemany(
                "INSERT INTO messages(session_key, seq, uuid, role, ts, text, tools)"
                " VALUES (?,?,?,?,?,?,?)",
                [(key, i, m["uuid"], m["role"], m["ts"], m["text"], m["tools"])
                 for i, m in enumerate(messages, start=1)],
            )
        counts["updated" if row is not None else "new"] += 1
    if not quiet:
        total = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        print("sync: %(new)d new, %(updated)d updated, %(unchanged)d unchanged, "
              "%(empty)d empty" % counts
              + (", %d shrunk (kept archive copy)" % counts["shrunk"] if counts["shrunk"] else "")
              + (", %d forgotten (skipped)" % counts["forgotten"] if counts["forgotten"] else "")
              + "; archive holds %d sessions" % total)
    return counts


# ---------------------------------------------------------------- search

def like_escape(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def one_line(text, limit=160):
    return re.sub(r"\s+", " ", text).strip()[:limit]


def snippet_around(text, term, width=70):
    flat = re.sub(r"\s+", " ", text)
    at = flat.lower().find(term.lower())
    if at < 0:
        return flat[:width * 2]
    start = max(0, at - width)
    return ("…" if start else "") + flat[start:at] + "[" + flat[at:at + len(term)] + "]" + \
        flat[at + len(term):at + len(term) + width] + "…"


def search(db, query, args):
    terms = query.split()
    if not terms:
        sys.exit("search: give at least one search term")
    use_like = [t for t in terms if db.tokenizer == "trigram" and len(t) < 3]
    use_fts = [t for t in terms if t not in use_like]

    where, params = [], []
    sql_from = "FROM messages m JOIN sessions s ON s.key = m.session_key"
    if use_fts:
        sql_from = ("FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                    "JOIN sessions s ON s.key = m.session_key")
        where.append("messages_fts MATCH ?")
        params.append(" AND ".join('"%s"' % t.replace('"', '""') for t in use_fts))
    for term in use_like:
        where.append("m.text LIKE ? ESCAPE '\\'")
        params.append("%" + like_escape(term) + "%")
    if not args.subagents:
        where.append("s.is_subagent = 0")
    if args.project:
        where.append("s.project LIKE ? ESCAPE '\\'")
        params.append("%" + like_escape(args.project) + "%")
    if args.since:
        where.append("substr(m.ts, 1, 10) >= ?")
        params.append(args.since)
    if args.until:
        where.append("substr(m.ts, 1, 10) <= ?")
        params.append(args.until)
    if args.role:
        where.append("m.role = ?")
        params.append(args.role)
    if use_fts:
        select = "SELECT m.session_key, m.seq, m.role, m.text, bm25(messages_fts) AS score"
        order = "ORDER BY score"
    else:
        select = "SELECT m.session_key, m.seq, m.role, m.text, 0 AS score"
        order = "ORDER BY m.ts DESC"
    rows = db.execute("%s %s WHERE %s %s LIMIT 2000" % (select, sql_from, " AND ".join(where), order),
                      params).fetchall()

    best = {}
    for row in rows:  # rows arrive best-first, so the first row per session is its best hit
        best.setdefault(row["session_key"], row)
    hits = list(best.values())[:args.limit]
    if not hits:
        print("no matches")
        LAST_SEARCH.write_text("[]")
        return
    saved = []
    for number, row in enumerate(hits, start=1):
        session = db.execute("SELECT * FROM sessions WHERE key=?", (row["session_key"],)).fetchone()
        date = (session["started_at"] or "")[:10]
        project = os.path.basename((session["project"] or "").rstrip("/")) or "?"
        print('%2d. %s  %s  "%s"  (%s)' % (number, date, project, session["title"], row["session_key"][:8]))
        print("    %s [%d]: %s" % (row["role"], row["seq"], snippet_around(row["text"], terms[0])))
        saved.append({"key": row["session_key"], "seq": row["seq"]})
    LAST_SEARCH.write_text(json.dumps(saved))
    print("\nLoad a hit with: recall.py show <number>   (more context: --turns N, --max-chars N)")


# ---------------------------------------------------------------- show / list / stats / forget

def resolve_session(db, target):
    """Return (session_key, hit_seq or None) for a hit number or a session id prefix."""
    if target.isdigit() and len(target) <= 3:
        try:
            hits = json.loads(LAST_SEARCH.read_text())
        except (OSError, ValueError):
            hits = []
        number = int(target)
        if 1 <= number <= len(hits):
            return hits[number - 1]["key"], hits[number - 1]["seq"]
        sys.exit("no hit %s in the last search (run `search` first, or pass a session id)" % target)
    rows = db.execute("SELECT key FROM sessions WHERE key LIKE ? ESCAPE '\\'",
                      (like_escape(target) + "%",)).fetchall()
    if not rows:
        sys.exit("no session matches %r" % target)
    if len(rows) > 1:
        sys.exit("%r matches %d sessions; use more characters" % (target, len(rows)))
    return rows[0]["key"], None


def show(db, args):
    key, hit_seq = resolve_session(db, args.target)
    session = db.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
    messages = db.execute(
        "SELECT seq, role, ts, text, tools FROM messages WHERE session_key=? AND seq > 0 ORDER BY seq",
        (key,)).fetchall()
    around = args.around if args.around is not None else hit_seq
    if args.all or around is None:
        start, end = 0, len(messages)
    else:  # message numbers are 1-based, list indexes are 0-based
        centre = max(around, 1)
        start, end = max(0, centre - 1 - args.turns), min(len(messages), centre + args.turns)

    print("# %s" % session["title"])
    print("session %s · %s · %s · %s" % (
        key, session["project"], session["git_branch"] or "-", (session["started_at"] or "")[:16]))
    used = 0
    for index in range(start, end):
        message = messages[index]
        block = "\n## [%d] %s · %s\n%s\n" % (
            message["seq"], message["role"], (message["ts"] or "")[:16], message["text"])
        if message["tools"]:
            block += "(tools: %s)\n" % message["tools"]
        if args.max_chars and index > start and used + len(block) > args.max_chars:
            print("\n… truncated at %d chars; continue with --around %d, or raise --max-chars"
                  % (args.max_chars, message["seq"]))
            return
        sys.stdout.write(block)
        used += len(block)


def list_sessions(db, args):
    rows = db.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_key = s.key AND m.seq > 0) AS n "
        "FROM sessions s WHERE s.is_subagent = 0 ORDER BY s.ended_at DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for row in rows:
        print('%s  %-22s  %4d msgs  "%s"  (%s)' % (
            (row["started_at"] or "")[:10], os.path.basename((row["project"] or "?").rstrip("/"))[:22],
            row["n"], row["title"], row["key"][:8]))


def stats(db):
    n_sessions = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    n_messages = db.execute("SELECT COUNT(*) FROM messages WHERE seq > 0").fetchone()[0]
    span = db.execute("SELECT MIN(started_at), MAX(ended_at) FROM sessions").fetchone()
    n_forgotten = db.execute("SELECT COUNT(*) FROM forgotten").fetchone()[0]
    print("database : %s (%.1f MB, tokenizer %s)" % (DB_PATH, DB_PATH.stat().st_size / 1e6, db.tokenizer))
    print("source   : %s" % SOURCE)
    print("sessions : %d   messages: %d   forgotten: %d" % (n_sessions, n_messages, n_forgotten))
    print("span     : %s .. %s" % ((span[0] or "-")[:10], (span[1] or "-")[:10]))


def forget(db, args):
    key, _ = resolve_session(db, args.target)
    session = db.execute("SELECT title FROM sessions WHERE key=?", (key,)).fetchone()
    if not args.yes:
        answer = input('Delete session %s "%s" from the archive? [y/N] ' % (key[:8], session["title"]))
        if answer.strip().lower() != "y":
            sys.exit("not deleted")
    with db:
        db.execute("DELETE FROM messages WHERE session_key=?", (key,))
        db.execute("DELETE FROM sessions WHERE key=?", (key,))
        db.execute("INSERT OR REPLACE INTO forgotten(key, forgotten_at) VALUES (?, ?)",
                   (key, datetime.datetime.utcnow().isoformat() + "Z"))
    print("forgot %s. It will not be re-imported by sync." % key)


# ---------------------------------------------------------------- launchd (macOS)

def plist_path():
    return HOME / "Library" / "LaunchAgents" / (JOB_LABEL + ".plist")


def job_python():
    # A venv's interpreter disappears with the venv; the base interpreter it was built from does not.
    return getattr(sys, "base_executable", None) or sys.executable


def plist_text(python):
    log = DB_PATH.parent / "sync.log"
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array>
    <string>%s</string><string>%s</string><string>sync</string><string>--quiet</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>%s</string>
  <key>StandardOutPath</key><string>%s</string>
</dict></plist>
""" % (JOB_LABEL, python, os.path.abspath(__file__), log, log)


def install_job(args):
    if sys.platform != "darwin":
        sys.exit("install-job only supports macOS (launchd). On Linux, add a cron line running: "
                 "%s %s sync --quiet" % (job_python(), os.path.abspath(__file__)))
    domain = "gui/%d" % os.getuid()
    python = args.python or job_python()
    if not args.python and ("venv" in python or "virtualenv" in python):
        print("warning: %s looks like a virtualenv and the job breaks if it is deleted;"
              " consider --python /usr/bin/python3" % python, file=sys.stderr)
    if args.dry_run:
        print("would write %s:\n%s\nthen run: launchctl bootstrap %s %s"
              % (plist_path(), plist_text(python), domain, plist_path()))
        return
    plist_path().parent.mkdir(parents=True, exist_ok=True)
    plist_path().write_text(plist_text(python))
    subprocess.call(["launchctl", "bootout", domain, str(plist_path())], stderr=subprocess.DEVNULL)
    subprocess.check_call(["launchctl", "bootstrap", domain, str(plist_path())])
    print("installed %s (daily at 12:00; runs on wake if the Mac was asleep)" % plist_path())


def uninstall_job(args):
    domain = "gui/%d" % os.getuid()
    subprocess.call(["launchctl", "bootout", domain, str(plist_path())], stderr=subprocess.DEVNULL)
    if plist_path().exists():
        plist_path().unlink()
    print("removed %s" % JOB_LABEL)


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(prog="recall.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="copy new/changed sessions into the archive")
    p.add_argument("--include-subagents", action="store_true")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("search", help="full-text search (syncs first unless --no-sync)")
    p.add_argument("query", nargs="+")
    p.add_argument("--project", help="substring of the project path")
    p.add_argument("--since", metavar="YYYY-MM-DD")
    p.add_argument("--until", metavar="YYYY-MM-DD")
    p.add_argument("--role", choices=["user", "assistant", "title"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--subagents", action="store_true", help="include subagent sessions")
    p.add_argument("--no-sync", action="store_true")

    p = sub.add_parser("show", help="print a passage or session from the archive")
    p.add_argument("target", help="hit number from the last search, or a session id prefix")
    p.add_argument("--around", type=int, metavar="SEQ", help="centre on message number SEQ")
    p.add_argument("--turns", type=int, default=3, help="messages of context each side (default 3)")
    p.add_argument("--all", action="store_true", help="whole session instead of a window")
    p.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                   help="size cap, about 4 chars per token; 0 = no cap (default %d)" % DEFAULT_MAX_CHARS)

    p = sub.add_parser("list", help="most recent sessions")
    p.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats", help="what the archive holds")

    p = sub.add_parser("forget", help="delete a session from the archive for good")
    p.add_argument("target", help="hit number or session id prefix")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    p = sub.add_parser("install-job", help="daily background sync via launchd (macOS)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--python", help="interpreter the job runs (default: the one running this script)")
    sub.add_parser("uninstall-job", help="remove the daily background sync")

    args = parser.parse_args(argv)
    if args.command == "install-job":
        return install_job(args)
    if args.command == "uninstall-job":
        return uninstall_job(args)

    db = connect()
    if args.command == "sync":
        sync(db, args.include_subagents, args.quiet)
    elif args.command == "search":
        if not args.no_sync:
            sync(db, quiet=True)
        search(db, " ".join(args.query), args)
    elif args.command == "show":
        show(db, args)
    elif args.command == "list":
        list_sessions(db, args)
    elif args.command == "stats":
        stats(db)
    elif args.command == "forget":
        forget(db, args)


if __name__ == "__main__":
    main()
