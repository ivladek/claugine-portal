"""The HA agent: what root does on an app node of the portal on two sites (see accounts/ha.py).

It runs as root (systemd `claugine-ha`, `manage.py ha_agent serve`) on this node's internal address, port HA_AGENT_PORT (portal.env),
over TLS (the portal's own certificate), and answers only requests that carry the shared token (HA_TOKEN in
portal.env, the same on both nodes); ufw lets only the other app node in.

    GET  /status            role, site, version, database (read only, GTIDs, replication), running tasks, last operation
    GET  /dump              the portal's database (mysqldump with GTIDs, gzip): for the other node to clone
    GET  /op                the last operation run here (steps)
    POST /role              {"role": …}
    POST /promote           stop replicating, writable
    POST /replicate         {"source": addr}: clone the database from source and replicate from it (read only)
    POST /copy              {"source": addr}: copy the database from source, writable, migrations (a new version)
    POST /install-from      {"source": addr}: install here, from GitHub, the commit source runs (no migrations)

The portal itself comes from GitHub only (deploy/server, claugine-portal-update); the nodes never pass code to each
other (2026-10-30).
    POST /wait-gtid         {"gtid": set, "timeout": s}: until this node has applied the set
    POST /note              {"action": …, "details": {…}}: write an entry to the portal's log here
    POST /op                {"name": …}: start an operation (switchover, to-new, new-to-active, new-to-standby,
                            maintenance-on, maintenance-off) — run here, step by step, talking to the other node

MySQL: GTID replication over TLS with the user `repl` (HA_REPL_PASSWORD), created on each node for the other node's
address only, outside the binary log. A standby is super_read_only.
"""
import gzip
import hashlib
import re
import json
import os
import shlex
import ssl
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone as dt_timezone

from django.conf import settings

from . import ha

PREFIX = "/opt/claugine-portal"
STATE_DIR = "/var/lib/claugine-ha"
OP_FILE = f"{STATE_DIR}/op.json"
TLS_DIR = "/etc/claugine-portal/tls"
REPL_USER = "repl"
OPS = ("switchover", "to-new", "new-to-active", "new-to-standby", "maintenance-on", "maintenance-off",
       "update-standby", "update-active", "standby-back")  # owner 2026-10-31: releases from GitHub, standby first
REF_OPS = ("update-standby", "update-active")
REF_RE = re.compile(r"^(v[0-9][0-9A-Za-z.\-]{0,40}|[0-9a-f]{40})$")  # a release tag or a commit


class AgentError(Exception):
    pass


# --- MySQL -------------------------------------------------------------------------------------------------

def sql(query, timeout=120):
    """Run SQL as root over the local socket; rows as lists of strings."""
    r = subprocess.run(["mysql", "--protocol=socket", "-uroot", "-N", "-B", "-e", query], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise AgentError(f"mysql: {(r.stderr.strip().splitlines() or ['failed'])[-1][:300]}")
    return [line.split("\t") for line in r.stdout.splitlines()]


def sql1(query):
    rows = sql(query)
    return rows[0][0] if rows and rows[0] else ""


def db_name():
    return settings.DATABASES["default"]["NAME"]


def repl_password():
    return os.environ.get("HA_REPL_PASSWORD", "")


def replica_status():
    try:
        r = subprocess.run(["mysql", "--protocol=socket", "-uroot", "-e", "SHOW REPLICA STATUS\\G"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        if ":" in line:
            k, _, v = line.strip().partition(":")
            out[k.strip()] = v.strip()
    if not out:
        return {}
    return {"source": out.get("Source_Host", ""), "io": out.get("Replica_IO_Running", ""), "sql": out.get("Replica_SQL_Running", ""),
            "behind": out.get("Seconds_Behind_Source", ""), "error": (out.get("Last_IO_Error") or out.get("Last_SQL_Error") or "")[:300]}


def db_status():
    try:
        return {"read_only": sql1("SELECT @@super_read_only") == "1", "gtid": executed_gtid(),
                "gtid_mode": sql1("SELECT @@GLOBAL.gtid_mode"), "server_id": sql1("SELECT @@server_id"),
                "replica": replica_status()}
    except (AgentError, OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)[:300]}


def set_read_only(on):
    """PERSIST: a standby stays read only after a MySQL restart, even before the agent runs again."""
    sql("SET PERSIST super_read_only=ON" if on else "SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF")


def enforce_role():
    """At the agent's start: a standby or a node in maintenance is read only (replication itself restarts with MySQL)."""
    from accounts import ha
    try:
        if ha.role() in ("standby", "maintenance") and not _ro():
            set_read_only(True)
    except (AgentError, OSError, subprocess.TimeoutExpired):
        pass


def stop_replica():
    try:
        sql("STOP REPLICA; RESET REPLICA ALL")
    except AgentError:
        pass


def start_replica(source):
    pw = repl_password()
    if not pw:
        raise AgentError("HA_REPL_PASSWORD is not set in portal.env")
    sql("CHANGE REPLICATION SOURCE TO SOURCE_HOST=%s, SOURCE_PORT=3306, SOURCE_USER=%s, SOURCE_PASSWORD=%s, "
        "SOURCE_AUTO_POSITION=1, SOURCE_SSL=1, GET_SOURCE_PUBLIC_KEY=1; START REPLICA"
        % (_q(source), _q(REPL_USER), _q(pw)))


def _q(v):
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def dump_stream():
    """mysqldump of the portal's database with its GTIDs, gzip, as a pipe."""
    cmd = (f"mysqldump --protocol=socket -uroot --single-transaction --set-gtid-purged=ON --routines --triggers "
           f"--events --databases {shlex.quote(db_name())} | gzip -c")
    return subprocess.Popen(["bash", "-o", "pipefail", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def load_from(source, replicate):
    """Replace the local database with source's: replicate (standby) or writable with migrations (a new version)."""
    stop_replica()
    set_read_only(False)
    name = db_name()
    sql(f"SET sql_log_bin=0; DROP DATABASE IF EXISTS `{name}`")
    sql("RESET BINARY LOGS AND GTIDS")
    resp = peer_request(source, "GET", "/dump", raw=True, timeout=1800)
    load = subprocess.Popen(["mysql", "--protocol=socket", "-uroot"], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    with gzip.GzipFile(fileobj=resp) as gz:
        while True:
            chunk = gz.read(1 << 20)
            if not chunk:
                break
            load.stdin.write(chunk)
    load.stdin.close()
    if load.wait() != 0:
        raise AgentError("loading the copy failed: " + load.stderr.read().decode(errors="replace")[-300:])
    if replicate:
        start_replica(source)
        set_read_only(True)
        return f"copied from {source}, replicating from it, read only"
    migrate()
    return f"copied from {source}, migrations applied, writable"


def migrate():
    r = subprocess.run(["/usr/local/bin/claugine-manage", "migrate", "--noinput"], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise AgentError("migrations failed: " + (r.stderr or r.stdout)[-300:])


def executed_gtid():
    """This server's executed GTID set in one line (mysql -N prints the newlines between server UUIDs as \\n)."""
    return "".join(sql1("SELECT @@GLOBAL.gtid_executed").replace("\\n", "").split())


def wait_gtid(gtid, timeout=120):
    if not gtid:
        return True
    return sql1(f"SELECT WAIT_FOR_EXECUTED_GTID_SET({_q(gtid)}, {int(timeout)})") == "0"


# --- version and distribution ---------------------------------------------------------------------------------

def version():
    try:
        with open(f"{PREFIX}/release", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


UPDATE = "/usr/local/sbin/claugine-portal-update"


def commit():
    """The GitHub commit installed here (40 hex), or "" (installed some other way)."""
    try:
        with open(f"{PREFIX}/COMMIT", encoding="utf-8") as fh:
            c = fh.read().strip()
    except OSError:
        return ""
    return c if len(c) == 40 and all(x in "0123456789abcdef" for x in c) else ""


def install_from(source):
    """Install here the commit the other node runs, from GitHub (no migrations: the database follows by replication or
    copy). The other node's code itself never comes over: both nodes get the portal from GitHub only."""
    want = (peer_request(source, "GET", "/status") or {}).get("commit") or ""
    if not want:
        raise AgentError(f"{source} was not installed from GitHub (no commit): install it with bootstrap.sh update first")
    if not os.path.exists(UPDATE):
        raise AgentError(f"{UPDATE} is missing here: run deploy/server/from-github.sh on this node first")
    r = subprocess.run([UPDATE, "--ref", want, "--no-migrate", "--no-agent-restart"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise AgentError("install failed: " + (r.stdout + r.stderr)[-400:])
    if commit() != want:
        raise AgentError(f"installed {commit()[:12] or 'nothing'}, expected {want[:12]}")
    _restart_later()
    return f"installed the version of {source} from GitHub: {want[:12]} (release {version()})"


def _restart_later():
    """The agent runs the new code after the operation: restarted a little later by systemd."""
    subprocess.run(["systemd-run", "--on-active=20", "--unit=claugine-ha-restart", "systemctl", "restart", "claugine-ha"],
                   capture_output=True, check=False)


_GITHUB = {"at": 0.0, "data": None}


def github_status(fresh=True):
    """What GitHub has (the release tags on the branch, newest first) and what is installed here:
    {branch, branch_head, installed, installed_tag, tags: [{tag, commit, date}], latest}. fresh=False: what was read in
    the last minute will do (the settings page)."""
    if not fresh and _GITHUB["data"] and time.time() - _GITHUB["at"] < 60:
        return _GITHUB["data"]
    if not os.path.exists(UPDATE):
        raise AgentError(f"{UPDATE} is missing here: run deploy/server/from-github.sh on this node first")
    r = subprocess.run([UPDATE, "--json"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise AgentError("GitHub: " + (r.stdout + r.stderr)[-300:])
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AgentError(f"GitHub: unreadable answer ({exc})") from exc
    _GITHUB.update(at=time.time(), data=data)
    return data


def install_ref(ref, migrate=False):
    """Install here a release (tag) or commit from GitHub. migrate: run the database changes (the active node only;
    a standby takes them by replication)."""
    if not REF_RE.match(ref or ""):
        raise AgentError(f"not a release tag or commit: {ref}")
    if not os.path.exists(UPDATE):
        raise AgentError(f"{UPDATE} is missing here: run deploy/server/from-github.sh on this node first")
    args = [UPDATE, "--ref", ref, "--no-agent-restart"] + ([] if migrate else ["--no-migrate"])
    r = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise AgentError("install failed: " + (r.stdout + r.stderr)[-400:])
    want = next((t["commit"] for t in github_status().get("tags", []) if t["tag"] == ref), ref)
    if commit() != want:
        raise AgentError(f"installed {commit()[:12] or 'nothing'}, expected {want[:12]}")
    _restart_later()
    return f"installed {ref} from GitHub: {want[:12]} (release {version()})"


# --- talking to the other node --------------------------------------------------------------------------------

def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE  # the nodes' self-signed certificates; the token authenticates
    return c


def peer_request(addr, method, path, body=None, raw=False, timeout=60):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"https://{addr}:{settings.HA_AGENT_PORT}{path}", data=data, method=method,
                                 headers={"X-Claugine-Token": settings.HA_TOKEN, "Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_ctx())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read() or b"{}").get("error", "")
        except ValueError:
            msg = ""
        raise AgentError(f"{addr}{path}: HTTP {exc.code} {msg}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AgentError(f"{addr}{path}: {getattr(exc, 'reason', exc)}") from exc
    if raw:
        return resp
    return json.loads(resp.read() or b"{}")


def status():
    n = ha.read_node()
    out = {"role": n.get("ROLE", "active"), "site": n.get("SITE", ""), "addr": n.get("ADDR", ""), "peer": n.get("PEER_ADDR", ""),
           "version": version(), "commit": commit(), "db": db_status(), "op": read_op(), "time": time.time()}
    try:
        from accounts.tasks import running_count
        out["tasks_running"] = running_count()
    except Exception as exc:  # noqa: BLE001
        out["tasks_running"] = None
        out["tasks_error"] = str(exc)[:200]
    return out


def note(action, details):
    from accounts.models import audit
    audit(None, action, None, **{k: (v if isinstance(v, (int, float, bool)) else str(v)[:300]) for k, v in (details or {}).items()})


# --- operations -----------------------------------------------------------------------------------------------

def read_op():
    try:
        with open(OP_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class Op:
    def __init__(self, name, by=""):
        self.data = {"name": name, "by": by, "state": "running", "steps": [], "started": _now(), "node": ha.read_node().get("SITE", "")}
        self.save()

    def save(self):
        os.makedirs(STATE_DIR, mode=0o750, exist_ok=True)
        tmp = OP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, OP_FILE)

    def step(self, name, fn):
        entry = {"name": name, "status": "running", "at": _now(), "detail": ""}
        self.data["steps"].append(entry)
        self.save()
        try:
            entry["detail"] = str(fn() or "")[:500]
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"], entry["detail"] = "failed", f"{exc}"[:500]
            raise
        finally:
            self.save()

    def finish(self, ok, error=""):
        self.data.update(state="done" if ok else "failed", error=error[:500], finished=_now())
        self.save()


def _now():
    return datetime.now(dt_timezone.utc).isoformat(timespec="seconds")


def set_role(r):
    if r not in ha.ROLES:
        raise AgentError(f"not a role: {r}")
    ha.write_node({"ROLE": r})
    return f"role {r}"


def _peer():
    p = ha.read_node().get("PEER_ADDR", "")
    if not p:
        raise AgentError("this node has no other node (PEER_ADDR in node.env)")
    return p


def run_op(name, by="", ref=""):
    """Run an operation here (in a thread of the agent). Steps are kept in op.json and noted in the portal's log."""
    if read_op().get("state") == "running":
        raise AgentError("another operation is running")
    if name not in OPS:
        raise AgentError(f"unknown operation {name}")
    op = Op(name, by)
    peer = None
    me = ha.read_node().get("ADDR", "")
    try:
        peer = _peer()
        p = peer_request(peer, "GET", "/status")
        if name == "switchover":
            _switchover(op, me, peer, p)
        elif name == "maintenance-on":  # the other node (a standby) is taken out, e.g. to work on its VM
            if p["role"] != "standby":
                raise AgentError(f"the other node is {p['role']}, not standby")
            op.step("other node: maintenance", lambda: peer_request(peer, "POST", "/role", {"role": "maintenance"})["result"])
        elif name == "maintenance-off":
            if p["role"] != "maintenance":
                raise AgentError(f"the other node is {p['role']}, not in maintenance")
            op.step("other node: standby", lambda: peer_request(peer, "POST", "/role", {"role": "standby"})["result"])
        elif name == "to-new":
            if p["role"] != "standby":
                raise AgentError(f"the other node is {p['role']}, not standby")
            op.step("other node: copy and migrate", lambda: peer_request(peer, "POST", "/copy", {"source": me}, timeout=3600)["result"])
            op.step("other node: role new", lambda: peer_request(peer, "POST", "/role", {"role": "new"})["result"])
        elif name == "new-to-active":
            if p["role"] != "new":
                raise AgentError(f"the other node is {p['role']}, not new")
            _check_no_tasks()
            op.step("this node: maintenance", lambda: set_role("maintenance"))
            op.step("this node: read only", lambda: set_read_only(True) or "database read only")
            op.step("new node: maintenance", lambda: peer_request(peer, "POST", "/role", {"role": "maintenance"})["result"])
            op.step("new node: final copy and migrations", lambda: peer_request(peer, "POST", "/copy", {"source": me}, timeout=3600)["result"])
            op.step("new node: active", lambda: peer_request(peer, "POST", "/role", {"role": "active"})["result"])
            op.step("this node: install the new version", lambda: install_from(peer))
            op.step("this node: clone and replicate", lambda: load_from(peer, replicate=True))
            op.step("this node: standby", lambda: set_role("standby"))
        elif name == "update-standby":  # owner 2026-10-31: a release onto the standby (no database changes there)
            if ha.read_node().get("ROLE", "active") != "active":
                raise AgentError("run it on the active node")
            if p["role"] != "standby":
                raise AgentError(f"the other node is {p['role']}, not standby")
            op.data["ref"] = ref
            op.step(f"other node: install {ref} from GitHub",
                    lambda: peer_request(peer, "POST", "/install-ref", {"ref": ref, "migrate": False}, timeout=3700)["result"])
            want = next((t["commit"] for t in github_status().get("tags", []) if t["tag"] == ref), ref)
            op.step("other node: check", lambda: _check_standby(peer, want))
        elif name == "update-active":  # the standby first: the active node only installs what the standby runs already
            if ha.read_node().get("ROLE", "active") != "active":
                raise AgentError("run it on the active node")
            want = next((t["commit"] for t in github_status().get("tags", []) if t["tag"] == ref), "")
            if not want:
                raise AgentError(f"{ref} is not a release on GitHub")
            if p["role"] != "standby" or p.get("commit") != want:
                raise AgentError(f"install {ref} on the standby first (it runs {str(p.get('commit') or '?')[:12]})")
            _check_no_tasks()
            op.data["ref"] = ref
            from accounts import appliance
            op.step("backup", lambda: _backup_or_fail(appliance))
            op.step(f"this node: install {ref} from GitHub (with the database changes)", lambda: install_ref(ref, migrate=True))
        elif name == "standby-back":  # the standby back to the version of the active node
            if p["role"] != "standby":
                raise AgentError(f"the other node is {p['role']}, not standby")
            op.step("other node: install this node's version from GitHub",
                    lambda: peer_request(peer, "POST", "/install-from", {"source": me}, timeout=3700)["result"])
        elif name == "new-to-standby":
            if p["role"] != "new":
                raise AgentError(f"the other node is {p['role']}, not new")
            op.step("other node: maintenance", lambda: peer_request(peer, "POST", "/role", {"role": "maintenance"})["result"])
            op.step("other node: install this version", lambda: peer_request(peer, "POST", "/install-from", {"source": me}, timeout=3600)["result"])
            op.step("other node: clone and replicate", lambda: peer_request(peer, "POST", "/replicate", {"source": me}, timeout=3600)["result"])
            op.step("other node: standby", lambda: peer_request(peer, "POST", "/role", {"role": "standby"})["result"])
        op.finish(True)
    except Exception as exc:  # noqa: BLE001 - kept in the operation
        op.finish(False, str(exc))
        if name == "switchover" and op.data.get("rollback"):
            try:
                set_read_only(False)
                set_role("active")
                op.data["error"] += "; this node is active again"
                op.save()
            except Exception:  # noqa: BLE001
                pass
    try:
        target = peer if (op.data["state"] == "done" and name in ("switchover", "new-to-active")) else None
        details = {"operation": name, "state": op.data["state"], "by": by, "error": op.data.get("error", ""),
                   "steps": "; ".join(f"{s['name']}: {s['status']}" for s in op.data["steps"])}
        if target:
            peer_request(target, "POST", "/note", {"action": f"ha_{name.replace('-', '_')}", "details": details})
        elif ha.read_node().get("ROLE") in ("active", "maintenance") and not _ro():
            note(f"ha_{name.replace('-', '_')}", details)
    except Exception:  # noqa: BLE001 - the log entry is a courtesy
        pass
    return op.data


def _check_standby(peer, want, wait=180):
    """After a new version on the standby: its agent (restarted) reports that commit, the database still replicates and
    the portal answers (the role endpoint is the portal itself, so the new code loads)."""
    deadline, last = time.time() + wait, "no answer"
    time.sleep(30)  # the agent restarts 20 s after the install
    port = (ha.read_node().get("STATUS_PORTS") or "80").split(",")[0].strip()
    while time.time() < deadline:
        try:
            p = peer_request(peer, "GET", "/status", timeout=15)
            rep = (p.get("db") or {}).get("replica") or {}
            if p.get("commit") != want:
                last = f"it runs {str(p.get('commit') or '?')[:12]}"
            elif rep.get("io") != "Yes" or rep.get("sql") != "Yes":
                last = f"it does not replicate: {rep.get('error') or rep}"
            else:
                code = urllib.request.urlopen(f"http://{peer}:{port}/standby", timeout=15).status
                if code == 200:
                    return f"runs {want[:12]}, replicates, the portal answers"
                last = f"the portal answers {code}"
        except AgentError as exc:
            last = str(exc)
        except urllib.error.HTTPError as exc:
            last = f"the portal answers {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            last = f"the portal: {getattr(exc, 'reason', exc)}"
        time.sleep(10)
    raise AgentError(f"the other node is not right after the install: {last}")


def _backup_or_fail(appliance):
    r = appliance.backup()
    if not str(r).startswith("ok"):
        raise AgentError(f"backup: {r}")
    return r


def _ro():
    try:
        return sql1("SELECT @@super_read_only") == "1"
    except AgentError:
        return True


def _check_no_tasks():
    from accounts.tasks import running_count
    n = running_count()
    if n:
        raise AgentError(f"{n} task(s) running: wait until they finish")


def _switchover(op, me, peer, p):
    mine = ha.read_node().get("ROLE", "active")
    if mine != "active":
        raise AgentError(f"this node is {mine}, not active")
    if p["role"] != "standby":
        raise AgentError(f"the other node is {p['role']}, not standby")
    if p.get("version") != version():
        raise AgentError(f"the other node runs another version ({p.get('version')} ≠ {version()}): install the same one first")
    rep = (p.get("db") or {}).get("replica") or {}
    if rep.get("io") != "Yes" or rep.get("sql") != "Yes":
        raise AgentError(f"the other node does not replicate: {rep.get('error') or rep}")
    _check_no_tasks()
    op.data["rollback"] = True
    op.step("this node: maintenance", lambda: set_role("maintenance"))
    time.sleep(3)  # requests in flight end
    op.step("this node: read only", lambda: set_read_only(True) or "database read only")
    gtid = executed_gtid()
    op.step("other node: caught up", lambda: _caught_up(peer, gtid))
    op.data["rollback"] = False  # from here the other node takes over
    op.step("other node: promote", lambda: peer_request(peer, "POST", "/promote")["result"])
    op.step("other node: active", lambda: peer_request(peer, "POST", "/role", {"role": "active"})["result"])
    op.step("this node: replicate from the other", lambda: start_replica(peer) or f"replicating from {peer}")
    op.step("this node: standby", lambda: set_role("standby"))


def _caught_up(peer, gtid):
    r = peer_request(peer, "POST", "/wait-gtid", {"gtid": gtid, "timeout": 120}, timeout=150)
    if not r.get("ok"):
        raise AgentError("the other node did not catch up in 120 s")
    return "all transactions applied there"


# --- the server ------------------------------------------------------------------------------------------------

def handle(method, path, body):
    """(status, dict) of one request (no streams)."""
    if method == "GET" and path == "/status":
        return 200, status()
    if method == "GET" and path == "/op":
        return 200, read_op()
    if method == "GET" and path == "/github":
        return 200, github_status(fresh=False)
    if method != "POST":
        return 404, {"error": "not found"}
    if path == "/role":
        return 200, {"result": set_role(body.get("role", ""))}
    if path == "/promote":
        stop_replica()
        set_read_only(False)
        return 200, {"result": "promoted: not replicating, writable"}
    if path == "/replicate":
        return 200, {"result": load_from(body["source"], replicate=True)}
    if path == "/copy":
        return 200, {"result": load_from(body["source"], replicate=False)}
    if path == "/install-ref":
        return 200, {"result": install_ref(body.get("ref", ""), migrate=bool(body.get("migrate")))}
    if path == "/install-from":
        return 200, {"result": install_from(body["source"])}
    if path == "/wait-gtid":
        return 200, {"ok": wait_gtid(body.get("gtid", ""), int(body.get("timeout", 120)))}
    if path == "/note":
        note(body.get("action", "ha_note"), body.get("details") or {})
        return 200, {"result": "noted"}
    if path == "/op":
        name = body.get("name", "")
        if read_op().get("state") == "running":
            return 409, {"error": "another operation is running"}
        if name not in OPS:
            return 400, {"error": f"unknown operation {name}"}
        ref = body.get("ref", "")
        if name in REF_OPS and not REF_RE.match(ref or ""):
            return 400, {"error": f"{name} needs a release tag"}
        threading.Thread(target=_op_thread, args=(name, body.get("by", ""), ref), daemon=True).start()
        return 202, {"result": f"{name} started"}
    return 404, {"error": "not found"}


def _op_thread(name, by, ref=""):
    from django.db import connection
    try:
        run_op(name, by, ref=ref)
    finally:
        connection.close()


def check_token(given):
    want = settings.HA_TOKEN
    return bool(want) and hashlib.sha256((given or "").encode()).digest() == hashlib.sha256(want.encode()).digest()


def serve(addr, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):  # no request lines (they would only show the paths)
            pass

        def _json(self, status_code, data):
            raw = json.dumps(data, default=str).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self):
            if not check_token(self.headers.get("X-Claugine-Token")):
                self._json(403, {"error": "forbidden"})
                return False
            return True

        def _stream(self, proc, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
                self.wfile.write(chunk)
            proc.wait()

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return
            if self.path == "/dump":
                return self._stream(dump_stream(), "application/gzip")
            self._reply("GET", {})

        def do_POST(self):  # noqa: N802
            if not self._auth():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json(400, {"error": "bad JSON"})
            self._reply("POST", body)

        def _reply(self, method, body):
            from django.db import connection
            try:
                code, data = handle(method, self.path, body)
            except Exception as exc:  # noqa: BLE001 - the caller sees what failed
                code, data = 500, {"error": str(exc)[:500]}
            finally:
                connection.close()
            self._json(code, data)

    httpd = ThreadingHTTPServer((addr, port), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(f"{TLS_DIR}/portal.crt", f"{TLS_DIR}/portal.key")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


# --- what the portal (the web pages) uses ------------------------------------------------------------------------

def nodes_status():
    """Both nodes as their agents report them: [(label, addr, status or {"error"})]."""
    n = ha.node()
    out = []
    for label, addr in (("this node", n.get("ADDR", "")), ("other node", n.get("PEER_ADDR", ""))):
        if not addr:
            continue
        try:
            out.append((label, addr, peer_request(addr, "GET", "/status", timeout=10)))
        except AgentError as exc:
            out.append((label, addr, {"error": str(exc)}))
    return out


def start_op(name, by, ref=""):
    """Ask this node's agent to run an operation (ref: the release of update-standby / update-active)."""
    addr = ha.node().get("ADDR", "") or "127.0.0.1"
    return peer_request(addr, "POST", "/op", {"name": name, "by": by, "ref": ref}, timeout=30)


def github_for_page():
    """The GitHub part of Settings → Portal, from this node's agent: github_status() or {"error"}."""
    addr = ha.node().get("ADDR", "") or "127.0.0.1"
    try:
        return peer_request(addr, "GET", "/github", timeout=30)
    except AgentError as exc:
        return {"error": str(exc)}

