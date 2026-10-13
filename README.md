# Scionfeld

**A version control system about nothing.**

> "What's the deal with packfiles?"

Anthropic [rebuilt source control](https://www.anthropic.com/engineering/redesigning-source-control) because their monorepo went from 2,500 merges a day to 30,000, with 100 GB of history and 25 million CI checkouts a day. Git couldn't keep up, so seven people built **Scion** in five months.

I don't have any of those problems. I built Scionfeld anyway.

It's called Scion**feld** because, like the show, nothing happens in it. I will never use it. You shouldn't either.

### Provenance

I made this **entirely on my phone, while eating Indian food and drinking beer.** Read the code with that in mind. Any bugs are the garlic naan's fault. Any off-by-one errors are the second IPA's fault. The tikka masala was excellent and had no part in this.

---

## Scion vs. Scionfeld

| | Scion | Scionfeld |
|---|---|---|
| Merges per day | 30,000+ | 0 (projected) |
| History size | ~100 GB | a few KB of jokes |
| Backing store | Spanner + cloud storage | a folder |
| Cache tiers | daemon → regional cache → server | a folder inside your folder |
| Built by | a team of 7 in 5 months | 1 guy on a phone in 1 dinner |
| Code review tool | Antique | `sfd review`, which quotes Seinfeld at you |
| CPU savings vs. git mirrors | ~200x | unmeasurable, because nobody runs it |
| Fuel | Claude | Claude, chicken tikka, beer |

## What it actually does

Somehow it works, and it really does copy Scion's design:

- **Clients are always online.** History lives on the server. Your working copy holds a cache and some files. If you go offline you get: *"Clients are always online. You, apparently, are not."*
- **Commits happen on the server. There is nothing to push.** `sfd push` exists only to tell you this.
- **Changesets live in a write-ahead log.** Each one lists the files changed relative to its first parent and points at zlib-compressed, sha256-addressed blobs. No trees, no packfiles.
- **Refs are discouraged** and only move by compare-and-swap.
- **The server rebases for you.** If `main` moved while you were working and nobody touched your files, the server rebases your change. If someone did: **"No merge for you! Come back, one year."**
- **Derived data.** Manifests and blame are computed from parents and cached the first time someone asks.
- **Sparse checkouts.** Only the paths you ask for get downloaded. The rest exist the way Kramer's first name existed for six seasons.
- **One RPC interface on every tier.** The client talks to a folder or to an HTTP server the same way.
- **Review with "semantic constraints".** A change that only touches comments is auto-approved: *"Yada yada yada, approved."*

## Install

You don't need anything but Python 3. No dependencies, no Spanner, no five months.

```sh
./sfd --help
```

## Usage (hypothetically)

```sh
sfd server init srv                  # "Spanner not included. It's a folder."
sfd clone srv wc                     # "No packfiles were harmed."
cd wc && echo hello > a.txt
sfd commit -m "the summer of george" # committed on the server
sfd push                             # there is nothing to push
sfd pull                             # move to the server's head
sfd status                           # "It's a show about nothing."
sfd diff
sfd log
sfd yada-yada                        # first changeset, "...yada yada yada...", latest
sfd blame a.txt                      # who did this
sfd festivus                         # the Airing of Grievances (blame for every line in the repo)
sfd review                           # Antique-ish review. Exits 1 if you're not spongeworthy.
sfd serenity-now                     # revert the head changeset. Insanity later.
sfd sparse src docs                  # change which paths get downloaded
sfd server serve srv --port 8642     # serve RPCs over HTTP for your zero teammates
sfd clone http://localhost:8642 wc2
```

Errors all start with **"No soup for you!"**

## Tests

```sh
python3 -m unittest discover -s tests
```

Ten passing tests, for software with zero users. It's the principle of the thing.

## FAQ

**Should I replace git with this?**
No. Anthropic says Scion isn't a general git replacement either, and they actually needed theirs.

**Is it production ready?**
It's dinner ready.

**Why?**
Not that there's anything wrong with that.

## License

Do whatever you want. Everybody else does.
