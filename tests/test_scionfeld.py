"""Tests for a VCS nobody will use. Thorough anyway. It's the principle of the thing."""

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scionfeld  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = scionfeld.main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue() + err.getvalue()


class ScionfeldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.environ["SFD_AUTHOR"] = "jerry"
        self.assertEqual(run("server", "init", "srv")[0], 0)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def clone(self, name, *extra):
        self.assertEqual(run("clone", "srv", name, *extra)[0], 0)

    def in_(self, name, *argv):
        os.chdir(os.path.join(self.tmp.name, name))
        try:
            return run(*argv)
        finally:
            os.chdir(self.tmp.name)

    def test_commit_on_server_and_nothing_to_push(self):
        self.clone("wc")
        self.write("wc/soup.txt", "no soup for you\n")
        code, out = self.in_("wc", "commit", "-m", "add soup")
        self.assertEqual(code, 0)
        self.assertIn("There is nothing to push", out)
        self.assertIn("nothing to push", self.in_("wc", "push")[1])
        self.assertIn("Nothing changed", self.in_("wc", "status")[1])

    def test_other_clone_sees_it_after_pull(self):
        self.clone("jerry")
        self.clone("george")
        self.write("jerry/a.txt", "serenity\n")
        self.in_("jerry", "commit", "-m", "a")
        self.assertEqual(self.in_("george", "pull")[0], 0)
        self.assertEqual(self.read("george/a.txt"), "serenity\n")

    def test_server_side_rebase_and_conflict(self):
        self.clone("jerry")
        self.clone("george")
        self.write("jerry/a.txt", "jerry\n")
        self.in_("jerry", "commit", "-m", "jerry's thing")
        self.write("george/b.txt", "george\n")
        code, out = self.in_("george", "commit", "-m", "george's thing")
        self.assertEqual(code, 0)
        self.assertIn("rebased", out)
        self.assertEqual(self.read("george/a.txt"), "jerry\n")  # landed on top of jerry
        self.clone("elaine")
        self.write("elaine/a.txt", "elaine\n")
        self.write("jerry/a.txt", "jerry v2\n")
        self.in_("jerry", "pull")
        self.write("jerry/a.txt", "jerry v2\n")
        self.in_("jerry", "commit", "-m", "v2")
        code, out = self.in_("elaine", "commit", "-m", "conflict")
        self.assertEqual(code, 1)
        self.assertIn("No merge for you", out)

    def test_blame_and_festivus(self):
        self.clone("wc")
        self.write("wc/f.txt", "one\ntwo\n")
        self.in_("wc", "commit", "-m", "jerry")
        os.environ["SFD_AUTHOR"] = "kramer"
        self.write("wc/f.txt", "one\ntwo\nthree\n")
        self.in_("wc", "commit", "-m", "kramer")
        _, out = self.in_("wc", "blame", "f.txt")
        lines = out.strip().splitlines()
        self.assertIn("jerry", lines[0])
        self.assertIn("kramer", lines[2])
        _, out = self.in_("wc", "festivus")
        self.assertIn("kramer: 1 line(s)", out)
        self.assertIn("jerry: 2 line(s)", out)

    def test_serenity_now_reverts(self):
        self.clone("wc")
        self.write("wc/f.txt", "calm\n")
        self.in_("wc", "commit", "-m", "calm")
        self.write("wc/f.txt", "SERENITY\n")
        self.in_("wc", "commit", "-m", "loud")
        code, out = self.in_("wc", "serenity-now")
        self.assertEqual(code, 0)
        self.assertEqual(self.read("wc/f.txt"), "calm\n")

    def test_revert_of_an_add_deletes(self):
        self.clone("wc")
        self.write("wc/new.txt", "x\n")
        self.in_("wc", "commit", "-m", "add")
        self.in_("wc", "serenity-now")
        self.assertFalse(os.path.exists("wc/new.txt"))

    def test_sparse_checkout(self):
        self.clone("full")
        self.write("full/src/a.py", "a\n")
        self.write("full/docs/b.md", "b\n")
        self.in_("full", "commit", "-m", "both")
        self.clone("sparse", "--sparse", "src")
        self.assertTrue(os.path.exists("sparse/src/a.py"))
        self.assertFalse(os.path.exists("sparse/docs/b.md"))
        # Not seeing docs/ must not be read as deleting it.
        self.assertIn("Nothing changed", self.in_("sparse", "status")[1])

    def test_review_semantic_constraint(self):
        self.clone("wc")
        self.write("wc/x.py", "x = 1\n")
        self.in_("wc", "commit", "-m", "code")
        self.write("wc/x.py", "# the sea was angry that day\nx = 1\n")
        self.in_("wc", "commit", "-m", "comment")
        _, out = self.in_("wc", "review")
        self.assertIn("Comment-only", out)

    def test_yada_yada(self):
        self.clone("wc")
        for i in range(5):
            self.write("wc/f.txt", f"{i}\n")
            self.in_("wc", "commit", "-m", f"c{i}")
        _, out = self.in_("wc", "yada-yada")
        self.assertIn("yada yada yada (4 changesets)", out)

    def test_http_transport(self):
        httpd = scionfeld.make_http_server("srv", 0, host="127.0.0.1")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_port}"
            self.assertEqual(run("clone", url, "wc")[0], 0)
            self.write("wc/f.txt", "over the wire\n")
            self.assertEqual(self.in_("wc", "commit", "-m", "remote")[0], 0)
            self.assertIn("remote", self.in_("wc", "log")[1])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
