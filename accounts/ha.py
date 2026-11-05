"""The portal on two sites: the role of this node and what it allows.

Each app VM has its own `/etc/claugine-portal/node.env` (never replicated, unlike the database):

    SITE=dc1            this node's site
    SERVER_ID=1         MySQL server id (unique per node)
    ADDR=10.81.101.25  this node's internal address
    PEER_SITE=dc2       the other app node (empty: one node only)
    PEER_ADDR=10.82.101.25
    LB_ADDRS=10.81.101.24,10.82.101.24   the HAProxy VMs allowed to reach the status page and the portal
    ROLE=active         active | standby | maintenance | new
    HTTP_PORT=8080      the portal's Apache ports on this node (install.sh keeps them here; defaults 8080, 8443)
    HTTPS_PORT=8443

Roles:
    active       serves every user; the timers (collector, health, history, backups) run here
    standby      serves nothing; its database replicates from the active node; same version as the active node
    maintenance  serves nothing (being switched over, or stopped on purpose)
    new          a new portal version under test (my-test…): only super admins sign in; its database is a copy taken
                 when it became "new"; mail is not sent and the platforms are read only from here

HAProxy asks each node GET /active, /standby, /maintenance, /new (internal; port 80, or the node's STATUS_PORTS, e.g. 55587, once
HAProxy runs on the app nodes themselves, 2026-10-30): 200 when the node has that role.
Without node.env the node is a single active portal, as before.
"""
import os
import time

from django.conf import settings

ROLES = ("active", "standby", "maintenance", "new")
STATUS_PATHS = {f"/{r}" for r in ROLES} | {f"/{r}/" for r in ROLES}
_CACHE = {"at": 0.0, "mtime": "unread", "data": {}}


def node_file():
    return getattr(settings, "NODE_ENV_FILE", "") or os.environ.get("NODE_ENV_FILE", "/etc/claugine-portal/node.env")


def read_node(path=None):
    data = {}
    try:
        with open(path or node_file(), encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
    except OSError:
        pass
    return data


def node():
    """This node's settings (re-read when node.env changes; checked at most every 2 seconds)."""
    now = time.monotonic()
    if now - _CACHE["at"] > 2:
        _CACHE["at"] = now
        try:
            mtime = os.path.getmtime(node_file())
        except OSError:
            mtime = None
        if mtime != _CACHE["mtime"]:
            _CACHE["mtime"], _CACHE["data"] = mtime, read_node()
    return _CACHE["data"]


def forget():
    _CACHE["at"], _CACHE["mtime"], _CACHE["data"] = 0.0, "unread", {}


def role():
    r = node().get("ROLE", "active")
    return r if r in ROLES else "maintenance"


def is_active():
    return role() == "active"


def has_peer():
    return bool(node().get("PEER_ADDR"))


def write_node(values, path=None):
    """Change node.env (root only: the HA agent and the installer). Keeps the other lines."""
    path = path or node_file()
    data = read_node(path)
    data.update({k: str(v) for k, v in values.items() if v is not None})
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("# This node of the Claugine portal (accounts/ha.py). Managed by the installer and the HA agent.\n")
        for k, v in data.items():
            fh.write(f"{k}={v}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    forget()
    return data


class ReadOnlyHere(Exception):
    pass


def forbid_writes(what):
    """Changing a platform, a router or an iLO is refused on a node that is not active (a new version under test)."""
    r = role()
    if r != "active":
        raise ReadOnlyHere(f"{what}: refused on this portal node ({r}): only the active portal changes the platforms")


def status_view(request, name):
    from django.http import HttpResponse
    r = role()
    resp = HttpResponse(f"{r}\n", content_type="text/plain", status=200 if r == name else 503)
    resp["Cache-Control"] = "no-store"
    return resp


class RoleMiddleware:
    """standby / maintenance: nothing but the status page; new: only super admins (and the sign-in page)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path in STATUS_PATHS:
            return self.get_response(request)
        from . import testmount
        if testmount.ON:  # the version under test: accounts.testmount.TestMountMiddleware decides
            return self.get_response(request)
        r = role()
        if r in ("standby", "maintenance"):
            return _page(request, 503, "maintenance")
        if r == "new":
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated and not getattr(user, "is_super_admin", False):
                from django.contrib.auth import logout
                logout(request)
                return _page(request, 403, "new")
        return self.get_response(request)


def _page(request, status, kind):
    from django.shortcuts import render
    try:
        return render(request, "ha/unavailable.html", {"kind": kind, "status": status}, status=status)
    except Exception:  # noqa: BLE001 - no template or database: plain text
        from django.http import HttpResponse
        return HttpResponse("The portal is under maintenance." if kind == "maintenance"
                            else "This is a new portal version under test: only super admins can sign in.",
                            status=status, content_type="text/plain")
