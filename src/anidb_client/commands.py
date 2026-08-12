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

from anidb_client.errors import AniDBIncorrectParameterError


class Command:
    def __init__(self, command, **parameters):
        self.command = command
        self.parameters = parameters
        self.raw = self.flatten(command, parameters)
        self.retries = 2

    def __repr__(self):
        return f"Command({self.tag!r},{self.command!r}) {self.parameters!r}\n{self.raw_data()}\n"

    def authorize(self, session):
        self.session = session

        self.parameters["tag"] = self.tag
        self.parameters["s"] = session

    def handle(self, resp):
        self.resp = resp
        self.callback(resp)

    def flatten(self, command, parameters):
        tmp = []
        for key, value in parameters.items():
            if value is None:
                continue
            tmp.append(f"{self.escape(key)}={self.escape(value)}")
        return " ".join([command, "&".join(tmp)])

    def escape(self, data):
        return str(data).replace("&", "&amp;")

    def raw_data(self):
        self.raw = self.flatten(self.command, self.parameters)
        return self.raw

    def handle_timeout(self, link):
        if self.retries > 0:
            self.retries -= 1
            link.request(self, self.callback, prio=True)
        else:
            link.set_banned(code=604, reason=b"API not responding")
            self.retries = 2
            link.request(self, self.callback, prio=True)


# first run
class AuthCommand(Command):
    def __init__(self, username, password, protover, client, clientver, nat=None, comp=1, enc="utf8", mtu=None):
        parameters = {
            "user": username,
            "pass": password,
            "protover": protover,
            "client": client,
            "clientver": clientver,
            "nat": nat,
            "comp": comp,
            "enc": enc,
            "mtu": mtu,
        }
        super().__init__("AUTH", **parameters)

    def handle_timeout(self, link):
        link.set_banned(code=604, reason=b"API not responding")


class LogoutCommand(Command):
    def __init__(self):
        super().__init__("LOGOUT")


# third run (at the same time as second)
class PushCommand(Command):
    def __init__(self, notify, msg, buddy=None):
        parameters = {"notify": notify, "msg": msg, "buddy": buddy}
        super().__init__("PUSH", **parameters)


class PushAckCommand(Command):
    def __init__(self, nid):
        parameters = {"nid": nid}
        super().__init__("PUSHACK", **parameters)


class NotifyAddCommand(Command):
    def __init__(self, aid=None, gid=None, type=None, priority=None):
        if not (aid or gid) or (aid and gid):
            raise AniDBIncorrectParameterError("You must provide aid OR gid for NOTIFICATIONADD command")
        parameters = {"aid": aid, "gid": gid, "type": type, "priority": priority}
        super().__init__("NOTIFICATIONADD", **parameters)


class NotifyCommand(Command):
    def __init__(self, buddy=None):
        parameters = {"buddy": buddy}
        super().__init__("NOTIFY", **parameters)


class NotifyListCommand(Command):
    def __init__(self):
        super().__init__("NOTIFYLIST")


class NotifyGetCommand(Command):
    def __init__(self, type, id):
        parameters = {"type": type, "id": id}
        super().__init__("NOTIFYGET", **parameters)


class NotifyAckCommand(Command):
    def __init__(self, type, id):
        parameters = {"type": type, "id": id}
        super().__init__("NOTIFYACK", **parameters)


class BuddyAddCommand(Command):
    def __init__(self, uid=None, uname=None):
        if not (uid or uname) or (uid and uname):
            raise AniDBIncorrectParameterError("You must provide <u(id|name)> for BUDDYADD command")
        parameters = {"uid": uid, "uname": uname.lower()}
        super().__init__("BUDDYADD", **parameters)


class BuddyDelCommand(Command):
    def __init__(self, uid):
        parameters = {"uid": uid}
        super().__init__("BUDDYDEL", **parameters)


class BuddyAcceptCommand(Command):
    def __init__(self, uid):
        parameters = {"uid": uid}
        super().__init__("BUDDYACCEPT", **parameters)


class BuddyDenyCommand(Command):
    def __init__(self, uid):
        parameters = {"uid": uid}
        super().__init__("BUDDYDENY", **parameters)


class BuddyListCommand(Command):
    def __init__(self, startat):
        parameters = {"startat": startat}
        super().__init__("BUDDYLIST", **parameters)


class BuddyStateCommand(Command):
    def __init__(self, startat):
        parameters = {"startat": startat}
        super().__init__("BUDDYSTATE", **parameters)


# first run
class AnimeCommand(Command):
    def __init__(self, aid=None, aname=None, amask=None):
        if not (aid or aname):
            raise AniDBIncorrectParameterError("You must provide <a(id|name)> for ANIME command")
        parameters = {"aid": aid, "aname": aname, "amask": amask}
        super().__init__("ANIME", **parameters)


class EpisodeCommand(Command):
    def __init__(self, eid=None, aid=None, aname=None, epno=None):
        if not (eid or ((aname or aid) and epno)) or (aname and aid) or (eid and (aname or aid or epno)):
            raise AniDBIncorrectParameterError("You must provide <eid XOR a(id|name)+epno> for EPISODE command")
        parameters = {"eid": eid, "aid": aid, "aname": aname, "epno": epno}
        super().__init__("EPISODE", **parameters)


class FileCommand(Command):
    def __init__(
        self,
        fid=None,
        size=None,
        ed2k=None,
        aid=None,
        aname=None,
        gid=None,
        gname=None,
        epno=None,
        fmask=None,
        amask=None,
    ):
        # Four commands below guard their parameters with an expression of this
        # shape, and it is worth reading once rather than four times.
        #
        # AniDB lets the same file be named several ways -- by its own id, by
        # size+ed2k, by anime+group+episode -- and accepts exactly one of them.
        # The expression says that as: one clause requiring some group to be
        # complete, then one clause per group requiring that nothing from any
        # *other* group was passed alongside it, then the id-versus-name pairs
        # that are two spellings of one value.
        #
        # It reads badly and it is deliberately left alone. Restating it as a
        # count of "which groups were supplied" is not faithful: MYLISTADD has a
        # fifth path (aid + generic + epno) that shares parameters with the
        # anime+group one, so the groups overlap and cannot simply be counted.
        # These guards are what stop a malformed command reaching an API that
        # bans clients, so they are changed only with a reason better than tidiness.
        if (
            not (fid or (size and ed2k) or ((aid or aname) and (gid or gname) and epno))
            or (fid and (size or ed2k or aid or aname or gid or gname or epno))
            or ((size and ed2k) and (fid or aid or aname or gid or gname or epno))
            or (((aid or aname) and (gid or gname) and epno) and (fid or size or ed2k))
            or (aid and aname)
            or (gid and gname)
        ):
            raise AniDBIncorrectParameterError(
                "You must provide <fid XOR size+ed2k XOR a(id|name)+g(id|name)+epno> for FILE command"
            )
        parameters = {
            "fid": fid,
            "size": size,
            "ed2k": ed2k,
            "aid": aid,
            "aname": aname,
            "gid": gid,
            "gname": gname,
            "epno": epno,
            "fmask": fmask,
            "amask": amask,
        }
        super().__init__("FILE", **parameters)


class GroupCommand(Command):
    def __init__(self, gid=None, gname=None):
        if not (gid or gname) or (gid and gname):
            raise AniDBIncorrectParameterError("You must provide <g(id|name)> for GROUP command")
        parameters = {"gid": gid, "gname": gname}
        super().__init__("GROUP", **parameters)


class GroupstatusCommand(Command):
    def __init__(self, aid=None, status=None):
        if not aid:
            raise AniDBIncorrectParameterError("You must provide aid for GROUPSTATUS command")
        parameters = {"aid": aid, "status": status}
        super().__init__("GROUPSTATUS", **parameters)


class ProducerCommand(Command):
    def __init__(self, pid=None, pname=None):
        if not (pid or pname) or (pid and pname):
            raise AniDBIncorrectParameterError("You must provide <p(id|name)> for PRODUCER command")
        parameters = {"pid": pid, "pname": pname}
        super().__init__("PRODUCER", **parameters)


class MyListCommand(Command):
    def __init__(self, lid=None, fid=None, size=None, ed2k=None, aid=None, aname=None, gid=None, gname=None, epno=None):
        if (
            not (lid or fid or (size and ed2k) or (aid or aname))
            or (lid and (fid or size or ed2k or aid or aname or gid or gname or epno))
            or (fid and (lid or size or ed2k or aid or aname or gid or gname or epno))
            or ((size and ed2k) and (lid or fid or aid or aname or gid or gname or epno))
            or ((aid or aname) and (lid or fid or size or ed2k))
            or (aid and aname)
            or (gid and gname)
        ):
            raise AniDBIncorrectParameterError(
                "You must provide <lid XOR fid XOR size+ed2k XOR a(id|name)+g(id|name)+epno> for MYLIST command"
            )
        parameters = {
            "lid": lid,
            "fid": fid,
            "size": size,
            "ed2k": ed2k,
            "aid": aid,
            "aname": aname,
            "gid": gid,
            "gname": gname,
            "epno": epno,
        }
        super().__init__("MYLIST", **parameters)


class MyListAddCommand(Command):
    def __init__(
        self,
        lid=None,
        fid=None,
        size=None,
        ed2k=None,
        aid=None,
        aname=None,
        gid=None,
        gname=None,
        epno=None,
        edit=None,
        state=None,
        viewed=None,
        viewdate=None,
        source=None,
        storage=None,
        other=None,
        generic=None,
    ):
        if (
            not (lid or fid or (size and ed2k) or ((aid or aname) and (gid or gname)) or (aid and generic and epno))
            or (lid and (fid or size or ed2k or aid or aname or gid or gname or epno))
            or (fid and (lid or size or ed2k or aid or aname or gid or gname or epno))
            or ((size and ed2k) and (lid or fid or aid or aname or gid or gname or epno))
            or (((aid or aname) and (gid or gname)) and (lid or fid or size or ed2k))
            or (aid and aname)
            or (gid and gname)
            or (lid and not edit)
        ):
            raise AniDBIncorrectParameterError(
                "You must provide <lid XOR fid XOR size+ed2k XOR a(id|name)+g(id|name)+epno> for MYLISTADD command"
            )
        parameters = {
            "lid": lid,
            "fid": fid,
            "size": size,
            "ed2k": ed2k,
            "aid": aid,
            "aname": aname,
            "gid": gid,
            "gname": gname,
            "generic": generic,
            "epno": epno,
            "edit": edit,
            "state": state,
            "viewed": viewed,
            "viewdate": viewdate,
            "source": source,
            "storage": storage,
            "other": other,
        }
        super().__init__("MYLISTADD", **parameters)


class MyListDelCommand(Command):
    def __init__(self, lid=None, fid=None, aid=None, aname=None, gid=None, gname=None, epno=None, size=None, ed2k=None):
        if (
            not (lid or fid or ((aid or aname) and epno))
            or (lid and (fid or aid or aname or gid or gname or epno))
            or (fid and (lid or aid or aname or gid or gname or epno))
            or (((aid or aname) and (gid or gname) and epno) and (lid or fid))
            or (aid and aname)
            or (gid and gname)
            or (ed2k and size)
        ):
            raise AniDBIncorrectParameterError(
                "You must provide <lid+edit=1 XOR fid XOR a(id|name)+epno> for MYLISTDEL command"
            )
        parameters = {
            "lid": lid,
            "fid": fid,
            "aid": aid,
            "aname": aname,
            "gid": gid,
            "gname": gname,
            "epno": epno,
            "size": size,
            "ed2k": ed2k,
        }
        super().__init__("MYLISTDEL", **parameters)


class MyListStatsCommand(Command):
    def __init__(self):
        super().__init__("MYLISTSTATS")


class VoteCommand(Command):
    def __init__(self, type, id=None, name=None, value=None, epno=None):
        if not (id or name) or (id and name):
            raise AniDBIncorrectParameterError("You must provide <(id|name)> for VOTE command")
        parameters = {"type": type, "id": id, "name": name, "value": value, "epno": epno}
        super().__init__("VOTE", **parameters)


class RandomAnimeCommand(Command):
    def __init__(self, type):
        parameters = {"type": type}
        super().__init__("RANDOMANIME", **parameters)


class PingCommand(Command):
    def __init__(self):
        super().__init__("PING")


# second run
class EncryptCommand(Command):
    def __init__(self, user, apipassword, type):
        self.apipassword = apipassword
        parameters = {"user": user.lower(), "type": type}
        super().__init__("ENCRYPT", **parameters)

    def handle_timeout(self, link):
        link.set_banned(code=604, reason=b"API not responding")


class EncodingCommand(Command):
    def __init__(self, name):
        # Was `{"name": type}` -- the builtin `type`, not the argument -- so this
        # command serialised as "ENCODING name=<class 'type'>" and ignored its
        # only parameter entirely.
        parameters = {"name": name}
        super().__init__("ENCODING", **parameters)


class SendMsgCommand(Command):
    def __init__(self, to, title, body):
        if len(title) > 50 or len(body) > 900:
            raise AniDBIncorrectParameterError(
                "Title must not be longer than 50 chars and body must not be longer than 900 chars for SENDMSG command"
            )
        parameters = {"to": to.lower(), "title": title, "body": body}
        super().__init__("SENDMSG", **parameters)


class UserCommand(Command):
    def __init__(self, user):
        parameters = {"user": user}
        super().__init__("USER", **parameters)


class UptimeCommand(Command):
    def __init__(self):
        super().__init__("UPTIME")


class VersionCommand(Command):
    def __init__(self):
        super().__init__("VERSION")
