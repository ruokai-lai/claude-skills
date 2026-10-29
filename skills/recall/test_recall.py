"""End-to-end tests for recall.py using synthetic transcripts. Run: python3 -m unittest -v test_recall"""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = str(Path(__file__).with_name("recall.py"))


def line(kind, content, ts, uuid, **extra):
    obj = {"type": kind, "uuid": uuid, "sessionId": "s", "timestamp": ts, "cwd": "/work/demo",
           "gitBranch": "main", "message": {"role": kind, "content": content}}
    obj.update(extra)
    return json.dumps(obj)


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "projects" / "-work-demo"
        self.source.mkdir(parents=True)
        self.db = self.tmp / "hist" / "history.db"
        self.env = dict(os.environ, RECALL_DB=str(self.db), RECALL_SOURCE=str(self.tmp / "projects"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp))

    def run_cli(self, *args, check=True):
        result = subprocess.run([sys.executable, SCRIPT] + list(args), env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        if check and result.returncode != 0:
            self.fail("recall %s failed: %s" % (" ".join(args), result.stderr))
        return result

    def write_session(self, name, lines):
        path = self.source / (name + ".jsonl")
        path.write_text("\n".join(lines) + "\n")
        return path

    def basic_lines(self):
        return [
            json.dumps({"type": "ai-title", "aiTitle": "Cache bug hunt", "sessionId": "s"}),
            line("user", "why is the cache invalidation broken", "2026-10-01T10:00:00Z", "u1"),
            line("assistant", [{"type": "text", "text": "The cache key ignores the tenant id."},
                               {"type": "tool_use", "name": "Edit",
                                "input": {"file_path": "/work/demo/cache.py"}}],
                 "2026-10-01T10:00:05Z", "a1"),
            line("user", [{"type": "tool_result", "content": "SECRETLESS tool output"}],
                 "2026-10-01T10:00:06Z", "u2"),
            line("assistant", "sidechain chatter", "2026-10-01T10:00:07Z", "a2", isSidechain=True),
            line("user", "缓存失效的问题修好了吗", "2026-10-01T10:01:00Z", "u3"),
        ]

    def count(self, sql):
        con = sqlite3.connect(str(self.db))
        try:
            return con.execute(sql).fetchone()[0]
        finally:
            con.close()

    def test_sync_is_idempotent_and_filters(self):
        self.write_session("sess1", self.basic_lines())
        first = self.run_cli("sync").stdout
        self.assertIn("1 new", first)
        second = self.run_cli("sync").stdout
        self.assertIn("0 new", second)
        self.assertIn("1 unchanged", second)
        # title row + 2 user + 1 assistant text; tool_result and sidechain skipped
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 4)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE text LIKE '%SECRETLESS%'"), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages WHERE text LIKE '%sidechain%'"), 0)
        tools = sqlite3.connect(str(self.db)).execute(
            "SELECT tools FROM messages WHERE role='assistant'").fetchone()[0]
        self.assertEqual(tools, "Edit /work/demo/cache.py")

    def test_growing_session_replaces_rows_without_duplicates(self):
        lines = self.basic_lines()
        path = self.write_session("sess1", lines)
        self.run_cli("sync")
        lines.append(line("user", "one more question", "2026-10-01T10:02:00Z", "u4"))
        path.write_text("\n".join(lines) + "\n")
        out = self.run_cli("sync").stdout
        self.assertIn("1 updated", out)
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 5)
        hits = self.run_cli("search", "one more question").stdout
        self.assertEqual(hits.count("Cache bug hunt"), 1)

    def test_archive_survives_source_deletion_and_shrinking(self):
        path = self.write_session("sess1", self.basic_lines())
        self.run_cli("sync")
        path.write_text(line("user", "tiny", "2026-10-01T10:00:00Z", "x") + "\n")
        out = self.run_cli("sync").stdout
        self.assertIn("1 shrunk", out)
        path.unlink()
        self.run_cli("sync")
        self.assertIn("cache key ignores", self.run_cli("search", "tenant").stdout)

    def test_redaction(self):
        secret = "sk-ant-api03-AbCdEf0123456789AbCdEf"
        lines = [line("user", "my key is %s and API_TOKEN=hunter2hunter2" % secret,
                      "2026-10-01T10:00:00Z", "u1")]
        self.write_session("sess1", lines)
        self.run_cli("sync")
        con = sqlite3.connect(str(self.db))
        text = con.execute("SELECT text FROM messages WHERE role='user'").fetchone()[0]
        con.close()
        self.assertNotIn(secret, text)
        self.assertNotIn("hunter2hunter2", text)
        self.assertIn("[REDACTED]", text)

    def test_search_english_cjk_and_short_terms(self):
        self.write_session("sess1", self.basic_lines())
        self.assertIn("Cache bug hunt", self.run_cli("search", "invalidation").stdout)
        self.assertIn("Cache bug hunt", self.run_cli("search", "缓存失效").stdout)
        self.assertIn("Cache bug hunt", self.run_cli("search", "缓存").stdout)  # 2 chars: LIKE path
        self.assertIn("no matches", self.run_cli("search", "nonexistentterm").stdout)
        self.assertIn("no matches", self.run_cli("search", "invalidation", "--project", "other").stdout)
        self.assertIn("no matches", self.run_cli("search", "invalidation", "--since", "2027-01-01").stdout)
        # a query containing FTS syntax characters must not raise
        self.run_cli("search", 'a"b', "OR", "NEAR(")

    def test_show_hit_window_and_cap(self):
        self.write_session("sess1", self.basic_lines())
        self.run_cli("search", "tenant")
        shown = self.run_cli("show", "1", "--turns", "0").stdout
        self.assertIn("cache key ignores the tenant id", shown)
        self.assertNotIn("缓存失效", shown)
        capped = self.run_cli("show", "sess1", "--max-chars", "50").stdout
        self.assertIn("truncated", capped)

    def test_forget_blocks_reimport(self):
        self.write_session("sess1", self.basic_lines())
        self.run_cli("sync")
        self.run_cli("forget", "sess1", "--yes")
        self.assertEqual(self.count("SELECT COUNT(*) FROM messages"), 0)
        out = self.run_cli("sync").stdout
        self.assertIn("0 new", out)
        self.assertIn("1 forgotten", out)
        self.assertIn("no matches", self.run_cli("search", "tenant").stdout)

    def test_subagents_are_opt_in(self):
        self.write_session("sess1", self.basic_lines())
        sub_dir = self.source / "sess1" / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-1.jsonl").write_text(
            line("assistant", "subagent found the zebra", "2026-10-01T10:00:00Z", "z1",
                 isSidechain=True) + "\n")
        self.run_cli("sync")
        self.assertIn("no matches", self.run_cli("search", "zebra").stdout)
        self.run_cli("sync", "--include-subagents")
        self.assertIn("no matches", self.run_cli("search", "zebra").stdout)
        self.assertIn("zebra", self.run_cli("search", "zebra", "--subagents").stdout)

    def test_private_permissions(self):
        self.write_session("sess1", self.basic_lines())
        self.run_cli("sync")
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode), 0o700)

    def test_empty_and_half_written_files_are_tolerated(self):
        self.write_session("empty", [json.dumps({"type": "file-history-snapshot"})])
        path = self.write_session("partial", self.basic_lines())
        with open(str(path), "a") as handle:
            handle.write('{"type": "user", "message": {"con')  # cut off mid-write
        out = self.run_cli("sync").stdout
        self.assertIn("1 new", out)
        self.assertIn("1 empty", out)


if __name__ == "__main__":
    unittest.main()
