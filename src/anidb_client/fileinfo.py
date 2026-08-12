#!/usr/bin/env python
#
# This file is part of anidb-client.
#
# anidb-client is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# anidb-client is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with anidb-client.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import os
import re
from collections.abc import Iterator
from types import TracebackType
from typing import IO, Any

from Crypto.Hash import MD4

try:
    import libnfs
except ImportError:
    libnfs = None

import anidb_client.errors

# `Any` rather than a protocol for the NFS handles and the libnfs context object.
# libnfs ships no type information at all (see pyproject's documented holes), so
# there is nothing to narrow to; writing a protocol here would describe a library
# this project cannot check itself against.

# None marks the breakpoint after which the regexes are fallbacks -- see below.
ep_nr_re: list[re.Pattern[str] | None] = [
    re.compile(
        r"[Ss]([0-9]+)[ ._-]*e([0-9]+)([0-9-]*)", re.IGNORECASE
    ),  # foo.s01.e01, foo.s01_e01, S01E02 foo, S01 - E02
    re.compile(r"[\._ -]()ep_?([0-9]+)([0-9-]*)", re.IGNORECASE),  # foo.ep01, foo.EP_01
    re.compile(r"[\\/\._ \[\(-]([0-9]{1,2})x([0-9]+)([0-9-]*)", re.IGNORECASE),  # foo.1x09* or just /1x09*
    re.compile(r"[/\._ \-](s)p(?:ecials?)?[._ \-]{0,3}([0-9]{1,3})([0-9-]*)", re.IGNORECASE),  # specials
    re.compile(r"[/\._ \-]{2}()([0-9]{1,4})([0-9-]*)", re.IGNORECASE),  # match '- nr' '-_nr' etc.
    re.compile(r"[/\._ \-](s)[\._ \-]{0,3}([0-9]{1,3})([0-9-]*)", re.IGNORECASE),  # specials
    None,  # the following regex are fallbacks and shouldn't be run if the
    # anime only has one episode. This None marks the breakpoint
    re.compile(r"[/\._ \-](s)p?(?:ecials?)?[\._ \-]{1,3}([0-9]{0,3})([0-9-]*)", re.IGNORECASE),
    # specials that may not have number
    re.compile(r"[/\._ \-](?:nc)?(o)p?(?:enings?)?[\._ \-]{0,3}([0-9]{0,3})([0-9-]*)", re.IGNORECASE),  # openings
    re.compile(r"[/\._ \-](?:nc)?(e)d?(?:ndings?)?[\._ \-]{0,3}([0-9]{0,3})([0-9-]*)", re.IGNORECASE),  # endings
    re.compile(r"[/\._ \-](t|pv)(?:railers?)?[\._ \-]{0,3}([0-9]{0,3})([0-9-]*)", re.IGNORECASE),  # trailers
    # "others"-type not implemented for now...
    re.compile(
        r"[/\._ \-]()([0-9]{1,4})([0-9-]*)", re.IGNORECASE
    ),  # if everything else fails, just match the first number(s)
]
partfile_re = re.compile(
    r"[/\._ \-](p)(?:ar)t[/\._ \-]{0,3}([0-9ivx]+)", re.IGNORECASE
)  # part-file, not complete episode/movie
multiep_re = re.compile(r"[0-9]+")
specials_re = re.compile(r"^(S|P|C|T|O)([0-9]+)$", re.IGNORECASE)


# http://www.radicand.org/blog/orz/2010/2/21/edonkey2000-hash-in-python/
def get_file_hash(path: str, nfs_obj: Any = None) -> str:
    if path.startswith("nfs://"):
        with NFSFile(path, "rb", nfs_obj) as f:
            return _calculate_ed2khash(f)
    with open(path, "rb") as f:
        return _calculate_ed2khash(f)


def _calculate_ed2khash(fileObj: IO[bytes]) -> str:
    """Returns the ed2k hash of a given file."""

    def gen(f: IO[bytes]) -> Iterator[bytes]:
        while True:
            x = f.read(9728000)
            if x:
                yield x
            else:
                return

    def md4_hash(data: bytes) -> MD4.MD4Hash:
        m = MD4.new()
        m.update(data)
        return m

    a = gen(fileObj)
    hashes = [md4_hash(data) for data in a]
    if not hashes:
        # An empty file yields no chunks at all, so the multi-chunk branch below
        # used to index hashes[0] and raise IndexError. ed2k defines the hash of
        # an empty file as MD4 of nothing, which is what a zero-chunk file is.
        return md4_hash(b"").hexdigest()
    if len(hashes) == 1:
        return hashes[0].hexdigest()
    # Above one chunk, ed2k hashes the concatenated chunk digests. This was a
    # functools.reduce over `a + d.digest()` seeded by overwriting hashes[0] with
    # its own digest -- which made the list hold two different kinds of thing and
    # is the same concatenation spelled out.
    return md4_hash(b"".join(h.digest() for h in hashes)).hexdigest()


def get_file_stats(path: str, nfs_obj: Any = None) -> tuple[datetime.datetime, int]:
    """Return (mtime, size). size is in bytes, mtime is a datetime object."""
    if path.startswith("nfs://"):
        return _nfs_stats(path, nfs_obj)

    stat = os.stat(path)

    size = stat.st_size
    mtime = datetime.datetime.fromtimestamp(stat.st_mtime)
    return (mtime, size)


class NFSFile:
    def __init__(self, path: str, mode: str, nfs_obj: Any = None) -> None:
        if not libnfs:
            raise anidb_client.errors.AniDBPathError("libnfs python module not installed, can't use nfs paths")
        self.mode = mode
        self.handle: Any = None
        self.nfs_obj = nfs_obj

        self.path = path

        if self.nfs_obj:
            if path.startswith(nfs_obj.url):
                self.rel_path = os.path.join("/", path[len(nfs_obj.url) :])
            else:
                self.rel_path = self.path

    def open(self) -> Any:
        if self.nfs_obj:
            self.handle = self.nfs_obj.open(self.rel_path, self.mode)
        else:
            self.handle = libnfs.open(self.path, self.mode)
        return self.handle

    def close(self) -> None:
        if self.handle:
            self.handle.close()

    def __enter__(self) -> Any:
        return self.open()

    # Renamed from `type`/`value`/`traceback`: the first shadowed the builtin it
    # needs in its own annotation. The interpreter passes these positionally.
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _nfs_stats(path: str, nfs_obj: Any = None) -> tuple[datetime.datetime, int]:
    with NFSFile(path, "r", nfs_obj) as f:
        stats = f.fstat()

    epoch = stats["mtime"]["sec"] + stats["mtime"]["nsec"] / 10**9
    return (datetime.datetime.fromtimestamp(epoch), stats["size"])
