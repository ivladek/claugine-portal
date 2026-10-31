"""Backups of the platforms' OpenNebula front ends (owner 2026-10-31, u04): what claugine-cli jobs/fe-backup.sh did from
cron on the admin hosts (fe_backup full=yes of every platform), now done by the portal.

Settings → Backups → Platform front ends: on/off, the time of the daily backup, how many days to keep them, the folders
of each front-end node. They go where the portal's own backups go (Settings → Backups: the local folder and/or the
S3 bucket), in a folder platforms/:

    platforms/<platform>_<VIP>_<YYYYmmdd-HHMM>/
        <server id>-<name>-<address>_<leader|follower>.zip   per front-end node, AES-256 with the archive password:
            config.tar.gz            the folders (by default /etc/one, /var/lib/one/.ssh, .one, remotes)
            onedb-zone.sql.gz        the leader: onedb backup of the zone's database
            onedb-federated.sql.gz   the leader of the federation's primary: onedb backup --federated
        MANIFEST                     the platform, its zone and servers, the files with their sizes and SHA-256

The nodes are the servers of the platform's zone (XML-RPC one.zone.info), the leader the one the VIP answers from
(one.zone.raftstatus). The portal logs in as the OS user oneadmin with the key of the platform's front-end secret
(Infrastructure → Secrets), reads only, and removes on the node only its own temporary dump. Old local backups are
removed (keep days) only after a run without errors; the S3 bucket keeps its own (its lifecycle rules).
Run by the appliance timer on the active node (as root), daily at the set time, or at once with "Back up now".
"""
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from django.utils import timezone

FOLDER = "platforms"
PREFIX = "platform-"


def _settings():
    from tenants.models import PortalSettings
    return PortalSettings.get()


def platforms(keys=None):
    """The platforms that can be backed up (a VIP, an endpoint and a front-end secret with the oneadmin ssh key);
    u06: narrowed to the chosen keys when `keys` is given."""
    from inventory.models import Platform
    out = [p for p in Platform.objects.exclude(fe_vip="").exclude(fe_secrets="").select_related("site").order_by("key")]
    return [p for p in out if p.key in keys] if keys else out


def dirs(s):
    return s.fe_backup_dir_list()


def due(s, now=None):
    """u06: every fe_backup_period_hours from fe_backup_time, or when requested ("Back up now")."""
    if s.fe_backup_requested_at:
        return True
    if not s.fe_backup:
        return False
    now = timezone.localtime(now or timezone.now())
    slot = now.replace(hour=s.fe_backup_time.hour, minute=s.fe_backup_time.minute, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    period = timedelta(hours=max(1, s.fe_backup_period_hours or 24))
    while slot + period <= now:  # the most recent slot at or before now
        slot += period
    last = timezone.localtime(s.fe_backup_last_at) if s.fe_backup_last_at else None
    return (last is None or last < slot) and now - slot < timedelta(hours=12)


def _nodes(p):
    """[(server id, name, address, role)] of the platform's zone; the leader first."""
    from adoption.collector import client_for
    from inventory.models import PlatformControl
    c = PlatformControl.objects.filter(key=p.key).first()
    if c is None:
        raise RuntimeError("no endpoint (Infrastructure → the platform)")
    cl = client_for(c)
    cfg = ET.fromstring(cl._call("one.system.config"))
    zone = int(cfg.findtext("FEDERATION/ZONE_ID") or 0)
    mode = (cfg.findtext("FEDERATION/MODE") or "STANDALONE").upper()
    z = ET.fromstring(cl._call("one.zone.info", zone))
    raft = ET.fromstring(cl._call("one.zone.raftstatus"))
    leader = raft.findtext("SERVER_ID") if raft.findtext("STATE") == "3" else None
    out = []
    from urllib.parse import urlsplit
    for srv in z.findall("SERVER_POOL/SERVER"):
        sid, name = srv.findtext("ID"), srv.findtext("NAME")
        addr = urlsplit(srv.findtext("ENDPOINT") or "").hostname or ""
        out.append((sid, name, addr, "leader" if sid == leader else "follower"))
    if not out:  # a front end without HA: the VIP is the node
        out = [("0", p.fe_name or p.key, p.fe_vip, "leader")]
    out.sort(key=lambda n: n[3] != "leader")
    return zone, mode, out


def _node_backup(p, node, key, work, password, folders, log):
    """One node's zip; returns (file name, errors)."""
    import pyzipper
    from inventory.sshexec import fetch, SshError
    sid, name, addr, role = node
    base = f"{sid}-{name}-{addr}_{role}"
    errors, parts = [], []
    tmp = tempfile.mkdtemp(prefix="fe-", dir=work)
    try:
        tgz = os.path.join(tmp, "config.tar.gz")
        quoted = " ".join("'" + d.replace("'", "") + "'" for d in folders)
        try:
            warn = fetch(addr, 22, "oneadmin", key,
                         f"tar -czf - --ignore-failed-read {quoted} 2>/tmp/.claugine-tar-$$; rc=$?; "
                         f"grep -v 'Removing leading' /tmp/.claugine-tar-$$ >&2; rm -f /tmp/.claugine-tar-$$; [ $rc -le 1 ]", tgz)
            parts.append((tgz, "config.tar.gz"))
            if warn:
                log(f"{p.key} {name}: {warn.splitlines()[-1][:160]}")
        except SshError as exc:
            errors.append(f"{name} config: {exc}")
        if role == "leader":
            kinds = [("zone", "")] + ([("federated", " --federated")] if p.fe_mode == "primary" else [])
            for kind, opt in kinds:
                out = os.path.join(tmp, f"onedb-{kind}.sql.gz")
                t = f"/tmp/claugine-onedb-{secrets.token_hex(6)}.sql"
                try:
                    fetch(addr, 22, "oneadmin", key, f"onedb backup {t}{opt} --force >/dev/null && gzip -c {t}; rc=$?; rm -f {t}; exit $rc", out,
                          timeout=3600)
                    parts.append((out, f"onedb-{kind}.sql.gz"))
                except SshError as exc:
                    errors.append(f"{name} onedb {kind}: {exc}")
        zname = f"{base}.zip"
        if parts:
            with pyzipper.AESZipFile(os.path.join(work, zname), "w", compression=pyzipper.ZIP_STORED,
                                     encryption=pyzipper.WZ_AES) as zf:
                zf.setpassword(password.encode())
                zf.setencryption(pyzipper.WZ_AES, nbits=256)
                for path, arc in parts:
                    zf.write(path, arc)
        return (zname if parts else ""), errors
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def backup_platform(p, folder, s, log=print):
    """One platform's backup folder in `folder`; returns (path, errors)."""
    from inventory import credentials as cr
    sec = cr.find(p.fe_secrets)
    key = sec.get("os_private_key") if sec is not None and sec.has("os_private_key") else ""
    if not key:
        return "", [f"{p.key}: no ssh key of oneadmin in its front-end secret"]
    password = s.get_secret("fe_backup_password") or s.get_secret("backup_password")  # u06: the platforms' own password
    if not password:
        return "", [f"{p.key}: no archive password (Settings → Backups)"]
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    final = os.path.join(folder, f"{PREFIX}{p.key}_{p.fe_vip}_{stamp}")
    work = os.path.join(folder, "." + os.path.basename(final) + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, mode=0o700)
    errors, files = [], []
    try:
        zone, mode, nodes = _nodes(p)
        for node in nodes:
            zname, errs = _node_backup(p, node, key, work, password, dirs(s), log)
            errors += errs
            if zname:
                files.append(zname)
        manifest = {"platform": p.key, "vip": p.fe_vip, "zone": zone, "federation": mode, "created": timezone.now().isoformat(),
                    "nodes": [{"id": n[0], "name": n[1], "address": n[2], "role": n[3]} for n in nodes],
                    "folders": dirs(s), "errors": errors,
                    "files": [{"name": f, "bytes": os.path.getsize(os.path.join(work, f)),
                               "sha256": _sha256(os.path.join(work, f))} for f in files]}
        with open(os.path.join(work, "MANIFEST"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)
        from .appliance import _fix_modes, _owner
        _fix_modes(work, _owner(folder))
        os.rename(work, final)
    except Exception as exc:  # noqa: BLE001 - kept in the result
        shutil.rmtree(work, ignore_errors=True)
        return "", errors + [f"{p.key}: {exc}"[:300]]
    return final, errors


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prune(folder, days):
    cut = time.time() - days * 86400
    n = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if name.startswith(PREFIX) and os.path.isdir(path) and os.path.getmtime(path) < cut:
            shutil.rmtree(path)
            n += 1
    return n


def run(s=None, log=print):
    """Every platform now (as root, the active node). Returns the result line, kept in Settings."""
    from accounts.models import audit
    from . import appliance, s3
    from tenants.models import PortalSettings
    s = s or _settings()
    started = timezone.now()
    PortalSettings.objects.filter(pk=s.pk).update(fe_backup_requested_at=None)
    tmp = tempfile.mkdtemp(prefix="claugine-fe-backup-")
    os.chmod(tmp, 0o700)
    done, errors = [], []
    try:
        local = bool((s.fe_backup_path or "").strip())  # u06: the platforms' own local folder and/or S3 bucket
        if not local and not s.fe_backup_s3:
            raise RuntimeError("no destination: set a local folder and/or an S3 bucket for the platform backups")
        folder = os.path.join(s.fe_backup_path, FOLDER) if local else tmp
        os.makedirs(folder, mode=appliance.DIR_MODE, exist_ok=True)
        s3conf = s3.conf_of_fe(s) if s.fe_backup_s3 else None  # u06: falls back to the portal bucket if empty
        s3prefix = (s.fe_backup_s3_prefix or s.backup_s3_prefix or "").strip("/")
        for p in platforms(s.fe_backup_platforms or None):
            path, errs = backup_platform(p, folder, s, log)
            errors += errs
            if not path:
                continue
            where = [path] if local else []
            try:
                if s3conf:
                    where.append(s3.upload_folder(s3conf, path, "/".join(x for x in (s3prefix, FOLDER) if x)))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{p.key}: copy: {exc}"[:300])
            done.append(f"{p.key} ({sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path)) // 1024} KiB)")
        if local and not errors:
            prune(folder, s.fe_backup_keep_days)
        took = int((timezone.now() - started).total_seconds())
        result = ((f"ok: {len(done)} platforms" if not errors else f"failed: {len(errors)} errors; {len(done)} platforms")
                  + f" in {took} s: " + ", ".join(done) + ("; " + "; ".join(errors) if errors else ""))[:2000]
    except Exception as exc:  # noqa: BLE001
        result = f"failed: {type(exc).__name__}: {exc}"[:1000]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    PortalSettings.objects.filter(pk=s.pk).update(fe_backup_last_at=started, fe_backup_last_result=result)
    audit(None, "platform_backup", s, ok=result.startswith("ok"), result=result[:300])
    if not result.startswith("ok"):  # u06: e-mail only on errors
        from django.utils.translation import gettext as _g
        appliance.notify_backup_failure(_g("Platform backups failed"), result)
    return result


def tick(log=print):
    s = _settings()
    if due(s):
        log("platform backups: " + run(s, log))
