#!/usr/bin/env python3
"""
Scionfeld: a version control system about nothing.

A loving, single-file, zero-dependency parody of Anthropic's Scion
(https://www.anthropic.com/engineering/redesigning-source-control),
faithful to the architecture and to absolutely none of the scale.

    Scion:      30,000 merges/day, 100 GB of history, 25M CI checkouts/day.
    Scionfeld:  1 user, who will never use it.

Architecture (yes, really):
  * Clients are always online. The server owns history; the client owns
    a cache and some opinions.
  * Commits happen on the server. There is nothing to push. Ever.
  * A changeset is an entry in a write-ahead log listing changed files
    relative to its first parent, pointing at content-addressed blobs.
  * Refs are discouraged. Moving one is a compare-and-swap.
  * Everything else (manifests, blame, grievances) is derived data,
    computed recursively from parents and memoized lazily.
  * Working copies are optional and sparse.
"""

import argparse
import difflib
import fnmatch
import getpass
import hashlib
import http.server
import json
import os
import sys
import threading
import time
import urllib.request
import zlib

VERSION = "0.0.0-about-nothing"
CLIENT_DIR = ".sfd"


class NoSoupForYou(Exception):
    """Raised whenever the server declines your request. Next!"""


# ---------------------------------------------------------------------------
# scionfeld-server: the authoritative tier (a directory and a dream)
# ---------------------------------------------------------------------------

class Server:
    """Owns the WAL, the objects, the refs, and the derived data."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.lock = threading.Lock()  # one lock, for one user, who isn't here

    # -- layout --------------------------------------------------------------

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def init(self):
        if os.path.exists(self._p("wal.jsonl")):
            raise NoSoupForYou("This server already exists. It's a show about nothing, not a show about two of the same thing.")
        for d in ("objects", "derived/manifest", "derived/blame"):
            os.makedirs(self._p(d), exist_ok=True)
        open(self._p("wal.jsonl"), "w").close()
        root_cs = self._append_changeset(
            parents=[], changes={}, author="jerry",
            message="Initial changeset. What's the deal with empty repositories?",
        )
        self._write_refs({"main": root_cs})
        return root_cs

    # -- objects (immutable, content-addressed, any cache may serve them) ----

    def put_blob(self, data_hex):
        data = bytes.fromhex(data_hex)
        h = hashlib.sha256(data).hexdigest()
        path = self._p("objects", h[:2], h[2:])
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(zlib.compress(data))
        return h

    def get_blobs(self, hashes):
        out = {}
        for h in hashes:
            path = self._p("objects", h[:2], h[2:])
            if not os.path.exists(path):
                raise NoSoupForYou(f"Blob {h[:12]} is missing. It's not a lie if you believe it.")
            with open(path, "rb") as f:
                out[h] = zlib.decompress(f.read()).hex()
        return out

    # -- the write-ahead log -------------------------------------------------

    def _wal(self):
        with open(self._p("wal.jsonl")) as f:
            return [json.loads(line) for line in f if line.strip()]

    def _changeset(self, cs_id):
        for cs in self._wal():
            if cs["id"].startswith(cs_id):
                return cs
        raise NoSoupForYou(f"No changeset {cs_id!r}. Yada yada yada, it doesn't exist.")

    def _append_changeset(self, parents, changes, author, message):
        body = {
            "parents": parents,
            "changes": dict(sorted(changes.items())),
            "author": author,
            "message": message,
            "time": time.time(),
        }
        body["id"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with open(self._p("wal.jsonl"), "a") as f:
            f.write(json.dumps(body, sort_keys=True) + "\n")
        return body["id"]

    # -- refs: discouraged, compare-and-swap ---------------------------------

    def _read_refs(self):
        with open(self._p("refs.json")) as f:
            return json.load(f)

    def _write_refs(self, refs):
        tmp = self._p("refs.json.tmp")
        with open(tmp, "w") as f:
            json.dump(refs, f, indent=2)
        os.replace(tmp, self._p("refs.json"))

    def resolve(self, ref):
        refs = self._read_refs()
        if ref in refs:
            return refs[ref]
        return self._changeset(ref)["id"]

    def cas_ref(self, ref, expected, new):
        with self.lock:
            refs = self._read_refs()
            if refs.get(ref) != expected:
                return {"ok": False, "actual": refs.get(ref)}
            refs[ref] = new
            self._write_refs(refs)
            return {"ok": True}

    # -- derived data: manifests ---------------------------------------------

    def manifest(self, cs_id):
        """path -> blob hash at a changeset. Derived recursively from parents."""
        cache = self._p("derived", "manifest", cs_id + ".json")
        if os.path.exists(cache):
            with open(cache) as f:
                return json.load(f)
        cs = self._changeset(cs_id)
        m = dict(self.manifest(cs["parents"][0])) if cs["parents"] else {}
        for path, blob in cs["changes"].items():
            if blob is None:
                m.pop(path, None)
            else:
                m[path] = blob
        with open(cache, "w") as f:
            json.dump(m, f)
        return m

    # -- the main event: server-side commits ---------------------------------

    def commit(self, ref, base, changes, author, message):
        """Create a changeset and move the ref. Nothing to push. Ever.

        If the ref moved under you, rebase on the server, as long as nobody
        touched the same files. Otherwise: no merge for you.
        """
        if not changes:
            raise NoSoupForYou("Nothing changed. Which, admittedly, is on brand.")
        for _ in range(10):
            head = self.resolve(ref)
            if head != base:
                theirs = self._changed_since(base, head)
                clash = sorted(set(theirs) & set(changes))
                if clash:
                    raise NoSoupForYou(
                        "No merge for you! Someone else changed: " + ", ".join(clash)
                        + ". Come back, one year. (Or ask Claude, it resolves these for a living.)"
                    )
            new = self._append_changeset([head], changes, author, message)
            if self.cas_ref(ref, head, new)["ok"]:
                return {"id": new, "rebased": head != base, "head": head}
        raise NoSoupForYou("The ref kept moving. Serenity now!")

    def _changed_since(self, base, head):
        changed, cur = set(), head
        while cur and cur != base:
            cs = self._changeset(cur)
            changed |= set(cs["changes"])
            cur = cs["parents"][0] if cs["parents"] else None
        return changed

    # -- history queries (source control as an API) --------------------------

    def log(self, ref, limit=50):
        out, cur = [], self.resolve(ref)
        while cur and len(out) < limit:
            cs = self._changeset(cur)
            out.append({k: cs[k] for k in ("id", "author", "message", "time", "changes", "parents")})
            cur = cs["parents"][0] if cs["parents"] else None
        return out

    def blame(self, ref, path):
        """Derived data: who wrote each line. Computed from parents, memoized."""
        head = self.resolve(ref)
        return self._blame_at(head, path)

    def _blame_at(self, cs_id, path):
        key = hashlib.sha256(f"{cs_id}:{path}".encode()).hexdigest()
        cache = self._p("derived", "blame", key + ".json")
        if os.path.exists(cache):
            with open(cache) as f:
                return json.load(f)
        cs = self._changeset(cs_id)
        parent = cs["parents"][0] if cs["parents"] else None
        if path not in cs["changes"]:
            result = self._blame_at(parent, path) if parent else []
        elif cs["changes"][path] is None:
            result = []
        else:
            new_lines = self._lines(cs["changes"][path])
            old = self._blame_at(parent, path) if parent else []
            old_lines = [entry["line"] for entry in old]
            result = []
            sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    result.extend(old[i1:i2])
                else:
                    result.extend(
                        {"line": l, "id": cs["id"], "author": cs["author"]} for l in new_lines[j1:j2]
                    )
        with open(cache, "w") as f:
            json.dump(result, f)
        return result

    def _lines(self, blob):
        return bytes.fromhex(self.get_blobs([blob])[blob]).decode("utf-8", "replace").splitlines()

    def revert(self, ref, author):
        """Serenity now: undo the head changeset, server-side."""
        head = self._changeset(self.resolve(ref))
        if not head["parents"]:
            raise NoSoupForYou("You can't revert the beginning. You can only yada yada it.")
        before = self.manifest(head["parents"][0])
        changes = {p: before.get(p) for p in head["changes"]}
        msg = f"SERENITY NOW! Revert \"{head['message']}\""
        return self.commit(ref, head["id"], changes, author, msg)

    # -- RPC dispatch --------------------------------------------------------

    RPCS = {"put_blob", "get_blobs", "resolve", "manifest", "commit", "log", "blame", "revert", "cas_ref"}

    def call(self, method, **kw):
        if method not in self.RPCS:
            raise NoSoupForYou(f"Unknown RPC {method!r}. Next!")
        return getattr(self, method)(**kw)


# ---------------------------------------------------------------------------
# Transport. Every tier speaks the same RPCs, so the client neither knows
# nor cares whether it is talking to a directory or an HTTP server.
# ---------------------------------------------------------------------------

class HTTPRemote:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def call(self, method, **kw):
        req = urllib.request.Request(
            f"{self.url}/rpc/{method}", data=json.dumps(kw).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                body = json.load(resp)
        except OSError as e:
            raise NoSoupForYou(
                f"Can't reach {self.url} ({e}). Clients are always online. "
                "You, apparently, are not."
            )
        if "error" in body:
            raise NoSoupForYou(body["error"])
        return body["result"]


def connect(where):
    if where.startswith(("http://", "https://")):
        return HTTPRemote(where)
    if not os.path.exists(os.path.join(where, "wal.jsonl")):
        raise NoSoupForYou(f"No Scionfeld server at {where}. Try `sfd server init {where}`.")
    return Server(where)


def make_http_server(root, port, host=""):
    server = Server(root)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            method = self.path.rsplit("/", 1)[-1]
            kw = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                payload = {"result": server.call(method, **kw)}
            except NoSoupForYou as e:
                payload = {"error": str(e)}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Scionfeld", "a-show-about-nothing")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            sys.stderr.write("[scionfeld-server] " + (fmt % args) + "\n")

    return http.server.ThreadingHTTPServer((host, port), Handler)


def serve(root, port):
    httpd = make_http_server(root, port)
    print(f"scionfeld-server listening on :{port}. Cache hit rate: 100% of 0 requests.")
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# sfd: the client. A bounded cache, some files, and an attitude.
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, workdir):
        self.workdir = os.path.abspath(workdir)
        self.meta = os.path.join(self.workdir, CLIENT_DIR)
        with open(os.path.join(self.meta, "config.json")) as f:
            self.config = json.load(f)
        self.remote = connect(self.config["server"])

    @classmethod
    def find(cls, start="."):
        d = os.path.abspath(start)
        while True:
            if os.path.isdir(os.path.join(d, CLIENT_DIR)):
                return cls(d)
            parent = os.path.dirname(d)
            if parent == d:
                raise NoSoupForYou("Not a Scionfeld working copy. Which is fine. Most places aren't.")
            d = parent

    def save(self):
        with open(os.path.join(self.meta, "config.json"), "w") as f:
            json.dump(self.config, f, indent=2)

    # -- the per-node "daemon": a content-addressed local cache --------------

    def fetch(self, hashes):
        cache = os.path.join(self.meta, "cache")
        os.makedirs(cache, exist_ok=True)
        want = [h for h in set(hashes) if not os.path.exists(os.path.join(cache, h))]
        if want:  # negotiation names only what we want, never what we have
            for h, data in self.remote.call("get_blobs", hashes=want).items():
                with open(os.path.join(cache, h), "wb") as f:
                    f.write(bytes.fromhex(data))
        out = {}
        for h in hashes:
            with open(os.path.join(cache, h), "rb") as f:
                out[h] = f.read()
        return out

    # -- sparse working copies -----------------------------------------------

    def in_view(self, path):
        pats = self.config.get("sparse") or []
        return not pats or any(path.startswith(p.rstrip("/") + "/") or path == p or fnmatch.fnmatch(path, p) for p in pats)

    def materialize(self, cs_id):
        old = self.remote.call("manifest", cs_id=self.config["base"]) if self.config.get("base") else {}
        new = self.remote.call("manifest", cs_id=cs_id)
        for path in old:
            if path not in new and self.in_view(path):
                full = os.path.join(self.workdir, path)
                if os.path.exists(full):
                    os.remove(full)
        wanted = {p: h for p, h in new.items() if self.in_view(p)}
        blobs = self.fetch(list(wanted.values()))
        for path, h in wanted.items():
            full = os.path.join(self.workdir, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(blobs[h])
        self.config["base"] = cs_id
        self.save()
        return len(wanted), len(new)

    def scan(self):
        files = {}
        for dirpath, dirnames, filenames in os.walk(self.workdir):
            dirnames[:] = [d for d in dirnames if d not in (CLIENT_DIR, ".git")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.workdir).replace(os.sep, "/")
                if self.in_view(rel):
                    with open(full, "rb") as f:
                        files[rel] = f.read()
        return files

    def status(self):
        base = self.remote.call("manifest", cs_id=self.config["base"])
        local = self.scan()
        changes = {}
        for path, data in local.items():
            if base.get(path) != hashlib.sha256(data).hexdigest():
                changes[path] = data
        for path in base:
            if self.in_view(path) and path not in local:
                changes[path] = None
        return base, changes


# ---------------------------------------------------------------------------
# Antique-ish code review. Approval rules with "semantic constraints".
# ---------------------------------------------------------------------------

COMMENT_PREFIXES = ("#", "//", "--", ";", "/*", "*", "*/", '"""', "'''")

VERDICTS = [
    "LGTM. It's real, and it's spectacular.",
    "Approved. These pretzels are making me thirsty.",
    "Approved, with reservations. You know how to TAKE the reservation, you just don't know how to HOLD the reservation.",
    "Changes requested. This diff is a Bizarro diff. Everything is the opposite.",
    "Approved. Hello, Newman.",
    "Changes requested. It's not you, it's me. No, actually, it's you.",
    "Approved. Giddy-up!",
    "Changes requested. I don't think you're spongeworthy yet.",
    "Approved. Not that there's anything wrong with that.",
]


def review(old_text, new_text, cs_id):
    diff = [l for l in difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    touched = [l[1:].strip() for l in diff if l[1:].strip()]
    if touched and all(l.startswith(COMMENT_PREFIXES) for l in touched):
        return "Comment-only change. Semantic constraint satisfied. Yada yada yada, approved."
    return VERDICTS[int(cs_id[:8], 16) % len(VERDICTS)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def short(h):
    return h[:10]


def when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def whoami():
    return os.environ.get("SFD_AUTHOR") or getpass.getuser()


def cmd_server_init(a):
    os.makedirs(a.path, exist_ok=True)
    root = Server(a.path).init()
    print(f"Initialized Scionfeld server at {a.path}")
    print(f"main -> {short(root)}  (Spanner not included. It's a folder.)")


def cmd_serve(a):
    serve(a.path, a.port)


def cmd_clone(a):
    remote = connect(a.server)
    dest = os.path.abspath(a.dest)
    os.makedirs(os.path.join(dest, CLIENT_DIR), exist_ok=True)
    server = a.server if a.server.startswith("http") else os.path.abspath(a.server)
    with open(os.path.join(dest, CLIENT_DIR, "config.json"), "w") as f:
        json.dump({"server": server, "ref": a.ref, "base": None, "sparse": a.sparse or []}, f, indent=2)
    c = Client(dest)
    head = remote.call("resolve", ref=a.ref)
    got, total = c.materialize(head)
    print(f"Checked out {a.ref} @ {short(head)} into {a.dest}")
    print(f"Materialized {got} of {total} files. No packfiles were harmed.")


def cmd_status(a):
    c = Client.find()
    _, changes = c.status()
    print(f"On {c.config['ref']} @ {short(c.config['base'])}")
    if not changes:
        print("Nothing changed. Perfect. It's a show about nothing.")
    for path, data in sorted(changes.items()):
        print(f"  {'deleted ' if data is None else 'modified'}  {path}")


def cmd_diff(a):
    c = Client.find()
    base, changes = c.status()
    old_blobs = c.fetch([base[p] for p in changes if p in base])
    for path, data in sorted(changes.items()):
        old = old_blobs[base[path]].decode("utf-8", "replace") if path in base else ""
        new = data.decode("utf-8", "replace") if data is not None else ""
        sys.stdout.writelines(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))


def cmd_commit(a):
    c = Client.find()
    _, changes = c.status()
    if a.paths:
        changes = {p: d for p, d in changes.items() if any(p == x or p.startswith(x.rstrip("/") + "/") for x in a.paths)}
    if not changes:
        raise NoSoupForYou("Nothing to commit. You're living in a society! Change something!")
    payload = {}
    for path, data in changes.items():
        payload[path] = None if data is None else c.remote.call("put_blob", data_hex=data.hex())
    res = c.remote.call("commit", ref=c.config["ref"], base=c.config["base"],
                        changes=payload, author=whoami(), message=a.message)
    note = f" (rebased onto {short(res['head'])} server-side, you're welcome)" if res["rebased"] else ""
    print(f"[{c.config['ref']} {short(res['id'])}] {a.message}{note}")
    print(f"{len(payload)} file(s) changed. Committed on the server. There is nothing to push.")
    c.materialize(res["id"])


def cmd_push(a):
    print("There is nothing to push.")
    print("Commits happen on the server. It's not a bug, it's the whole architecture.")
    print("Push is the Kramer of VCS commands: it bursts in, but nobody actually needs it.")


def cmd_pull(a):
    c = Client.find()
    _, changes = c.status()
    if changes:
        raise NoSoupForYou("You have uncommitted changes. Commit them first, or `sfd serenity-now` your feelings.")
    head = c.remote.call("resolve", ref=c.config["ref"])
    if head == c.config["base"]:
        print("Already up to date. Nothing happened. Nothing ever happens.")
        return
    got, _ = c.materialize(head)
    print(f"Moved to {short(head)}. Re-materialized {got} file(s), lazily, like a Sunday.")


def cmd_log(a):
    c = Client.find()
    for cs in c.remote.call("log", ref=a.ref or c.config["ref"], limit=a.n):
        print(f"changeset {cs['id']}")
        print(f"Author: {cs['author']}")
        print(f"Date:   {when(cs['time'])}")
        print(f"\n    {cs['message']}\n")


def cmd_yada_yada(a):
    c = Client.find()
    history = c.remote.call("log", ref=c.config["ref"], limit=10_000)
    if len(history) <= 2:
        for cs in history:
            print(f"{short(cs['id'])}  {cs['message']}")
        return
    first, last = history[-1], history[0]
    print(f"{short(first['id'])}  {first['message']}")
    print(f"            ...and yada yada yada ({len(history) - 2} changesets)...")
    print(f"{short(last['id'])}  {last['message']}")


def cmd_blame(a):
    c = Client.find()
    for entry in c.remote.call("blame", ref=c.config["ref"], path=a.path):
        print(f"{short(entry['id'])} {entry['author'][:12]:<12} | {entry['line']}")


def cmd_festivus(a):
    """The Airing of Grievances: who wrote what, and I've got a lot of problems with you people."""
    c = Client.find()
    manifest = c.remote.call("manifest", cs_id=c.config["base"])
    tally = {}
    for path in manifest:
        for entry in c.remote.call("blame", ref=c.config["ref"], path=path):
            tally[entry["author"]] = tally.get(entry["author"], 0) + 1
    print("Welcome, newcomers. The tradition of Festivus begins with the Airing of Grievances.\n")
    for author, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {author}: {n} line(s). I've got a lot of problems with you, {author}, and now you're gonna hear about it!")
    if not tally:
        print("  No lines. No grievances. This is a Festivus miracle.")
    print("\nThe Feats of Strength are not implemented. Until you pin me, this isn't over.")


def cmd_review(a):
    c = Client.find()
    target = a.changeset or c.config["base"]
    cs = next((x for x in c.remote.call("log", ref=c.config["ref"], limit=10_000) if x["id"].startswith(target)), None)
    if cs is None:
        raise NoSoupForYou(f"No changeset {target!r} on {c.config['ref']}.")
    before = c.remote.call("manifest", cs_id=cs["parents"][0]) if cs["parents"] else {}
    print(f"Antique-ish review of {short(cs['id'])}: {cs['message']}  (by {cs['author']})\n")
    worst = None
    for path, blob in cs["changes"].items():
        old = c.fetch([before[path]])[before[path]].decode("utf-8", "replace") if path in before else ""
        new = c.fetch([blob])[blob].decode("utf-8", "replace") if blob else ""
        verdict = review(old, new, cs["id"])
        print(f"  {path}: {verdict}")
        if verdict.startswith("Changes requested"):
            worst = verdict
    print("\nReviewer checked the intent, not every line. That's the new way.")
    if worst:
        sys.exit(1)


def cmd_serenity_now(a):
    c = Client.find()
    res = c.remote.call("revert", ref=c.config["ref"], author=whoami())
    print("SERENITY NOW!")
    print(f"Reverted the head changeset as {short(res['id'])}. Insanity later.")
    c.materialize(res["id"])


def cmd_sparse(a):
    c = Client.find()
    c.config["sparse"] = a.patterns
    c.save()
    got, total = c.materialize(c.config["base"])
    print(f"Sparse view now: {a.patterns or 'everything'}. {got}/{total} files materialized.")
    print("The rest are like Kramer's first name: they exist, you just haven't asked yet.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="sfd", description="Scionfeld: a VCS about nothing.")
    p.add_argument("--version", action="version", version=f"sfd {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("server", help="server things")
    ss = s.add_subparsers(dest="server_cmd", required=True)
    x = ss.add_parser("init", help="create a server (a folder)")
    x.add_argument("path")
    x.set_defaults(fn=cmd_server_init)
    x = ss.add_parser("serve", help="serve RPCs over HTTP")
    x.add_argument("path")
    x.add_argument("--port", type=int, default=8642)
    x.set_defaults(fn=cmd_serve)

    x = sub.add_parser("clone", help="make a (sparse, optional) working copy")
    x.add_argument("server")
    x.add_argument("dest")
    x.add_argument("--ref", default="main")
    x.add_argument("--sparse", nargs="*", help="only materialize these paths/globs")
    x.set_defaults(fn=cmd_clone)

    x = sub.add_parser("status", help="what changed (probably nothing)")
    x.set_defaults(fn=cmd_status)
    x = sub.add_parser("diff", help="show local changes")
    x.set_defaults(fn=cmd_diff)

    x = sub.add_parser("commit", help="commit on the server. There is nothing to push.")
    x.add_argument("-m", "--message", required=True)
    x.add_argument("paths", nargs="*")
    x.set_defaults(fn=cmd_commit)

    x = sub.add_parser("push", help="does nothing, eloquently")
    x.set_defaults(fn=cmd_push)
    x = sub.add_parser("pull", help="move your working copy to the server's head")
    x.set_defaults(fn=cmd_pull)

    x = sub.add_parser("log", help="history, as an API")
    x.add_argument("ref", nargs="?")
    x.add_argument("-n", type=int, default=20)
    x.set_defaults(fn=cmd_log)
    x = sub.add_parser("yada-yada", help="the log, but you skip the boring middle part")
    x.set_defaults(fn=cmd_yada_yada)

    x = sub.add_parser("blame", help="derived data: who did this")
    x.add_argument("path")
    x.set_defaults(fn=cmd_blame)
    x = sub.add_parser("festivus", help="the Airing of Grievances (blame, for everyone)")
    x.set_defaults(fn=cmd_festivus)

    x = sub.add_parser("review", help="Antique-ish code review with semantic constraints")
    x.add_argument("changeset", nargs="?")
    x.set_defaults(fn=cmd_review)

    x = sub.add_parser("serenity-now", help="revert the head changeset, server-side")
    x.set_defaults(fn=cmd_serenity_now)

    x = sub.add_parser("sparse", help="change which paths are materialized")
    x.add_argument("patterns", nargs="*")
    x.set_defaults(fn=cmd_sparse)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except NoSoupForYou as e:
        print(f"No soup for you! {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
