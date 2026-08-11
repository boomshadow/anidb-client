# anidb-client

An object-oriented UDP client library for [AniDB](https://anidb.net).

`anidb-client` wraps the AniDB UDP API in ordinary Python objects — `Anime`,
`Episode`, `File`, `Group` — and keeps an aggressive local cache in front of it,
because the UDP API is strictly rate-limited and will ban clients that talk to it
too often. You ask for an attribute; the library serves it from cache when it can
and goes to the network only when it must.

The typical use is mylist management: identifying local files by ed2k hash and
adding, editing or removing them from your AniDB mylist.

```python
import anidb_client

anidb_client.init("sqlite:///anidb.db", api_user="myuser", api_pass="mypassword")

anime = anidb_client.Anime("Kemono no Souja Erin")
print(f"{anime.title} has {anime.nr_of_episodes} episodes and is a {anime.type}")

anidb_client.close()
```

## Lineage

This is an independent fork of [adbb](https://github.com/winterbird-code/adbb)
by Winterbird, which was itself forked from `adba`. Considerable thanks are owed
to those projects — the protocol handling, the caching design and the
title-matching heuristics here all originate with them.

It is a hard fork, not a soft one. This project tracks no upstream, makes its own
API decisions, and is narrower in scope: the `arrange_anime`,
`jellyfin_anime_sync` and `adbb_cache` command-line tools that upstream ships are
deliberately **not** part of this package. `anidb-client` is a library.

## Requirements

* Python 3.11 or newer
* A SQLAlchemy-compatible database for the cache:
  * SQLite (simplest; a file is all you need)
  * PostgreSQL
  * MySQL / MariaDB
* An AniDB account

Runtime dependencies (`pycryptodome`, `sqlalchemy`) are installed automatically.

## Installation

```console
pip install anidb-client
```

Reading files over NFS (`File(path="nfs://...")`) additionally needs the
[`libnfs`](https://pypi.org/project/libnfs/) Python module. It is not declared as
an extra because its only release is a source distribution that compiles against
libnfs system headers, so installing it is left to you:

```console
pip install libnfs   # requires libnfs development headers
```

Without it, only local paths work; nothing else is affected.

## Registering a client with AniDB

**AniDB will not authenticate an unregistered client.** The `AUTH` command carries
a client name and an integer client version, and that pair must be registered
through [AniDB's client registration](https://wiki.anidb.net/UDP_API_DEV) before
it will work. If you are embedding this library in your own application, register
your own client and set it at init time:

```python
anidb_client.init(..., client_name="myclient", client_version=1)
```

The defaults identify this library. They are unrelated to the version of the
package itself — a `pip install --upgrade` does not change the identity AniDB
sees.

## Rate limits and bans, briefly

AniDB's UDP API is unusually strict, and the consequence for getting it wrong is
a temporary IP ban rather than an error response. The library defends against
this on your behalf, and it is worth knowing how:

* Requests are paced automatically (a short delay between commands, longer once a
  burst builds up). You cannot send faster by asking.
* Every response is cached in your database. The shortest caching period is one
  day; beyond that a probability score decides whether to refresh, so a large
  collection does not re-fetch everything at once.
* On a ban or server-busy response the client backs off exponentially rather than
  retrying immediately.
* The anime-titles and anime-list XML files are fetched over HTTPS at most once
  every 36 hours and cached on disk.

If you are testing an integration, test against a fake server rather than the
real API. This repository's own suite does exactly that and never sends a packet
off the loopback interface.

## Caching

All information fetched from AniDB is cached in the SQL database you pass to
`init()`. The shortest caching period is one day. After that, a probability score
based on the age of the data decides whether a given object is refreshed — the
intent is that a cache warms up over time instead of expiring all at once. The
scoring is heuristic and unlikely to be optimal for every use case.

You can always force a refresh with an object's `update()` method.

Anime title search uses the `anime-titles.xml.gz` file published by AniDB. It is
downloaded automatically and stored in the system temporary directory
(`/var/tmp/anime-titles.xml.gz` on POSIX systems), then reused for 36 hours
before being refreshed. Deleting the cached file forces an immediate update.

tvdb / tmdb / imdb mapping comes from
[Anime-Lists](https://github.com/Anime-Lists/anime-lists), cached the same way as
`/var/tmp/anime-list.xml`.

## Usage

```python
import anidb_client

# The database URL is the first argument. Credentials may be passed directly or
# read from a netrc file (see below).
anidb_client.init(
    "sqlite:///anidb.db",
    api_user="<anidb-username>",
    api_pass="<anidb-password>",
)

# An Anime can be created from a title or from an AniDB anime ID.
anime = anidb_client.Anime("Kemono no Souja Erin")
# anime = anidb_client.Anime(6187)

# "Kemono no Souja Erin has 50 episodes and is a TV Series"
print(f"{anime.title} has {anime.nr_of_episodes} episodes and is a {anime.type}")

# An Episode can be created from anime + episode number, or from an AniDB eid.
episode = anidb_client.Episode(anime=anime, epno=5)
# episode = anidb_client.Episode(eid=96461)

# "'Kemono no Souja Erin' episode 5 has title 'Erin and the Egg Thieves'"
print(f"'{episode.anime.title}' episode {episode.episode_number} has title '{episode.title_eng}'")

# A File can be created from a local path, an AniDB file ID, or anime + episode.
file = anidb_client.File(path="/media/Anime/Kemono no Souja Erin/[BD] Kemono no Souja Erin - 05.mkv")
# file = anidb_client.File(fid=<some-fid>)
# file = anidb_client.File(anime=anime, episode=episode)

# This usually works even for a file AniDB has never seen.
print(f"'{file.path}' contains episode {file.episode.episode_number} of "
      f"'{file.anime.title}'. Mylist state is '{file.mylist_state}'")

# Posters for Anime and Group objects.
# NOTE: the AniDB CDN has added a CAPTCHA, so this is unreliable. See Fanart below.
with open("poster.jpg", "wb") as f:
    anidb_client.download_image(f, anime)

# Always close the UDP session before exiting, so the client logs out cleanly.
anidb_client.close()
```

### init()

```python
anidb_client.init(
    sql_db_url,
    api_user=None,
    api_pass=None,
    debug=False,
    loglevel="info",
    logger=None,
    netrc_file=None,
    outgoing_udp_port=None,
    api_key=None,
    fanart_api_key=None,
    db_only=False,
    client_name=None,
    client_version=None,
)
```

`sql_db_url` is a SQLAlchemy URL and is the only required argument. Credentials
come either from `api_user`/`api_pass` or from a [netrc file](#netrc). Pass
`db_only=True` to work entirely from cache without opening a UDP session, and
`client_name`/`client_version` to authenticate as your own
[registered client](#registering-a-client-with-anidb).

## Reference

### Anime

```python
Anime(init)
```

`init` is either a title or an aid. Titles are matched against
`anime-titles.xml` using fuzzy text matching (via `difflib`), and only the single
best match becomes an `Anime`. Some titles are ambiguous: a search for `Ranma`
may return either `Ranma 1/2` (which has "Ranma" as a synonym) or
`Ranma 1/2 Nettou Hen` (which has it as an official title).

#### Attributes

* `aid` — AniDB anime ID
* `titles` — every title for this anime
* `title` — the main title
* `updated` — when this anime was last fetched from AniDB
* `tvdbid` — TVDB ID, or `None`. TV series only.
* `tmdbid` — TMDB *movie* ID, or `None`. May be a list when the anime maps to
  several movies; use `Episode.tmdbid` for a specific episode, and `extid()` for
  TV series.
* `imdbid` — IMDB ID, or `None`. May be a list when the anime maps to several
  movies; use `Episode.imdbid` for a specific episode. Movies only.
* `relations` — a list of `(relation_type, Anime)` tuples
* `fanart` — if [enabled](#fanart), a list of dicts translated directly from the
  [fanart.tv API](https://fanarttv.docs.apiary.io/). Empty list if not enabled.

The following attributes are returned from the AniDB API: `year`, `type`,
`nr_of_episodes`, `highest_episode_number`, `special_ep_count`, `air_date`,
`end_date`, `url`, `picname`, `rating`, `vote_count`, `temp_rating`,
`temp_vote_count`, `average_review_rating`, `review_count`, `is_18_restricted`,
`ann_id`, `allcinema_id`, `animenfo_id`, `anidb_updated`, `special_count`,
`credit_count`, `other_count`, `trailer_count`, `parody_count`.

#### Methods

```python
extid(source, id_type="tv")
```

Return external ID(s) for this anime. Valid `id_type` values are `'tv'` and
`'movie'`. Valid sources are `'thetvdb'` (tv only), `'tmdb'` (tv and movie) and
`'imdb'` (movie only). May return a list when the anime links to several titles
at the source, or `None` when no valid mapping exists for the combination.

```python
related_anime(exclude=None, only_in_mylist=True)
```

Walk this anime's relations transitively and return the connected set, starting
with the anime itself. `exclude` is an iterable of `Anime` treated as walls:
neither returned nor traversed through. While `only_in_mylist` is set the walk
follows only anime already in your mylist, which stops one sequel link from
dragging in an entire franchise.

### Episode

```python
Episode(anime=None, epno=None, eid=None)
```

Create from `anime` + `epno`, or from `eid` alone. `anime` may be a title, an aid
or an `Anime` object. `epno` is a string or int; `eid` is an int.

#### Attributes

* `eid` — AniDB episode ID
* `anime` — the `Anime` this episode belongs to
* `episode_number` — the episode number (note: a string)
* `updated` — when this episode was last fetched from AniDB
* `tvdb_episode` — `(season, episode)` if the episode maps to a TVDB episode.
  `episode` is usually an int, but may be an `(episode_number, part_number)`
  tuple or a list of ints when an AniDB episode maps to part of a TVDB episode
  or vice versa.
* `tmdb_episode` — as above, mapped to TMDB
* `tmdbid` — TMDB ID for this episode, or `None`
* `imdbid` — IMDB ID for this episode, or `None`

The following attributes are returned from the AniDB API: `length`, `rating`,
`votes`, `title_eng`, `title_romaji`, `title_kanji`, `aired`, `type`.

### File

```python
File(path=None, fid=None, anime=None, episode=None)
```

Requires `path`, `fid`, or `anime` and `episode`. When given `anime` and
`episode`, the file is either a generic file or whatever you have in your mylist
for that anime and episode.

Given a `path`, the library first checks the file's size and ed2k hash against
AniDB. If the file exists there, the `File` represents it. If it does not, the
library infers which anime and episode the file contains: the episode number is
guessed from the filename by regex, and if none is found and the anime has only
one episode, episode `1` is assumed. The anime title is guessed from the parent
directory when that matches `anime-titles.xml` well enough, and from the filename
otherwise. See `_guess_anime_ep_from_file()` and `_guess_epno_from_filename()` in
`animeobjs.py`, and `get_titles()` in `anames.py`.

#### Methods

```python
update_mylist(state=None, watched=None, source=None, other=None)
remove_from_mylist()
```

`update_mylist()` both adds and edits. `state` is one of `'unknown'`, `'on hdd'`,
`'on cd'` or `'deleted'`. `watched` is `True`, `False`, or a `datetime` recording
when it was watched.

#### Attributes

* `anime` — the `Anime` this file contains
* `episode` — the `Episode` this file contains
* `group` — `Group` object for the release group
* `multiep` — list of episode numbers this file contains. Filename parsing
  supports multi-episode files but the AniDB API does not, so this is not
  reliable.
* `fid` — AniDB file ID
* `path` — full path (when created from a path)
* `size` — file size in bytes
* `ed2khash` — ed2k hash, which is what AniDB identifies files by
* `updated` — when this file was last fetched from AniDB

The following attributes are returned from the AniDB API: `lid`, `gid`,
`is_deprecated`, `is_generic`, `crc_ok`, `file_version`, `censored`,
`length_in_seconds`, `description`, `aired_date`, `mylist_state`,
`mylist_filestate`, `mylist_viewed`, `mylist_viewdate`, `mylist_storage`,
`mylist_source`, `mylist_other`.

### Group

```python
Group(name=None, gid=None)
```

Requires a `name` (short or long) or a `gid`. A group created from a name is
always considered valid and is saved to the database even when the name matches
no AniDB group; in that case both `name` and `short` are set to the given name
and the other attributes stay empty.

#### Attributes

* `updated` — when this group was last fetched from AniDB

The following attributes are returned from the AniDB API: `gid`, `rating`,
`votes`, `acount`, `fcount`, `name`, `short`, `irc_channel`, `irc_server`, `url`,
`picname`, `founded`, `disbanded`, `dateflag`, `last_release`, `last_activity`.

## Fanart

`Anime.fanart` fetches available fanart from [fanart.tv](https://fanart.tv) when
two conditions are met:

* you provide an [API key](https://fanart.tv/get-an-api-key/), either as the
  `fanart_api_key` argument to `init()` or via a [netrc file](#netrc)
* the series or movie is mapped to a tvdb/tmdb/imdb ID in
  [Anime-Lists](https://github.com/Anime-Lists/anime-lists)

The attribute returns metadata translated directly from the fanart.tv API, so
consult [their reference](https://fanarttv.docs.apiary.io/) for its structure —
it differs slightly between series and movies. Use `download_fanart()` to fetch
the images themselves.

```python
import anidb_client

anidb_client.init("sqlite:///anidb.db", netrc_file=".netrc", fanart_api_key="secret")

anime = anidb_client.Anime("Kemono no Souja Erin")
background_url = anime.fanart[0]["showbackground"][0]["url"]

with open("background.jpg", "wb") as f:
    # preview=True downloads a low-resolution version instead.
    anidb_client.download_fanart(f, background_url, preview=False)

anidb_client.close()
```

## netrc

Rather than passing credentials to `init()`, they can be read from a
[netrc](https://everything.curl.dev/usingcurl/netrc) file via the `netrc_file`
argument. The library looks for:

* AniDB username, password and [encryption key](#encryption). The `account`
  field holds the encryption key. The machine name must be one of
  `api.anidb.net`, `api.anidb.info` or `anidb.net`.
* Database credentials — machine name must match the hostname in `sql_db_url`,
  and only the hostname: no port, and no brackets around an IPv6 literal
  (`machine ::1`, not `machine [::1]:5432`). Matching is case-insensitive. This
  lookup only happens when the URL carries no password of its own; a password
  already in the URL is left alone.
* fanart.tv API key — machine name must be one of `fanart.tv`,
  `assets.fanart.tv`, `webservice.fanart.tv` or `api.fanart.tv`.

```netrc
machine api.anidb.net
        login <anidb-username>
        password <anidb-password>
        account <anidb-encryption-key>
machine sql.example.com
        login <database-username>
        password <database-password>
machine fanart.tv
        account <fanart-api-key>
```

## Encryption

Per the [UDP API specification](https://wiki.anidb.net/UDP_API_Definition), an
encrypted session is not enabled by default and must be turned on by the user.
Provide your encryption key as the `api_key` argument to `init()` or via a
[netrc file](#netrc). You choose the key yourself in your
[AniDB profile](https://anidb.net/perl-bin/animedb.pl?show=profile).

## Development

Everything runs in Docker; nothing needs to be installed on your machine beyond
Docker and [Task](https://taskfile.dev).

```console
task build          # build the development image
task test           # run the test suite
task test:cov       # ...with a coverage report
task lint           # ruff
task format         # ruff format
task typecheck      # mypy
task spell          # codespell
task check          # everything CI runs
```

The test suite never contacts AniDB. A fake UDP server on loopback stands in for
the real API, and an autouse fixture fails any test that tries to open a socket
or make an HTTP request to a non-loopback address. Please keep it that way — a
test that reaches the real API risks an IP ban for whoever runs it next.

Dependencies are pinned exactly and hash-locked in `uv.lock`, and the resolver
enforces a 45-day cooldown on new releases.

## Upgrading

### Object API

The object API is intended to stay stable; code using `Anime`, `Episode`, `File`
and `Group` should keep working across releases.

### Database

The cache has no migration story. **Recreate the database after upgrading** —
delete the SQLite file, or drop and recreate the PostgreSQL/MySQL database. The
cache repopulates from AniDB as it is used.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
