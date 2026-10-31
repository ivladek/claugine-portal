"""Backups to an S3 bucket (owner 2026-10-31, u04): a small S3 client (AWS Signature Version 4) without extra packages.

The portal only puts objects: each file of a backup folder goes to s3://<bucket>/<prefix>/<backup folder>/<file>. It
never deletes anything there: how long backups stay is the bucket's (versioning, object lock retention, lifecycle
rules). Each upload carries Content-MD5, which buckets with object lock require.

Works with AWS S3 and S3-compatible storage (Ceph RGW, MinIO, …): path-style addressing by default
(https://<endpoint>/<bucket>/<key>), virtual-hosted style (https://<bucket>.<endpoint>/<key>) as an option.
"""
import base64
import hashlib
import hmac
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone as dt_timezone

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class S3Error(Exception):
    pass


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret, date, region, service="s3"):
    k = _hmac(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _quote_path(path):
    return urllib.parse.quote(path, safe="/~")


def sign(method, host, path, headers, payload_hash, access, secret, region, when=None, query=""):
    """The headers of a signed request (Authorization, x-amz-date, x-amz-content-sha256 and the given ones)."""
    when = when or datetime.now(dt_timezone.utc)
    amz_date, date = when.strftime("%Y%m%dT%H%M%SZ"), when.strftime("%Y%m%d")
    h = {k.lower(): str(v).strip() for k, v in headers.items()}
    h.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
    names = sorted(h)
    canonical = "\n".join([method, _quote_path(path), query, "".join(f"{n}:{h[n]}\n" for n in names), ";".join(names),
                           payload_hash])
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret, date, region), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    h["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={';'.join(names)}, "
                          f"Signature={signature}")
    return h


def _target(endpoint, bucket, key, style):
    u = urllib.parse.urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
    if u.scheme != "https":
        raise S3Error("the endpoint must be https")
    base = (u.path or "").rstrip("/")
    if style == "virtual":
        return u.scheme, f"{bucket}.{u.netloc}", f"{base}/{key}"
    return u.scheme, u.netloc, f"{base}/{bucket}/{key}"


def put_object(conf, key, data, timeout=300):
    """Put one object. conf: endpoint, region, bucket, access, secret, style."""
    scheme, host, path = _target(conf["endpoint"], conf["bucket"], key.lstrip("/"), conf.get("style") or "path")
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()  # noqa: S324 - Content-MD5 is MD5 by definition
    headers = sign("PUT", host, path, {"content-md5": md5, "content-length": len(data),
                                       "content-type": "application/octet-stream"},
                   hashlib.sha256(data).hexdigest(), conf["access"], conf["secret"], conf.get("region") or "us-east-1")
    req = urllib.request.Request(f"{scheme}://{host}{_quote_path(path)}", data=data, method="PUT",
                                 headers={k: v for k, v in headers.items() if k != "host"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        body = (exc.read() or b"")[:400].decode(errors="replace")
        code = body.split("<Code>")[1].split("</Code>")[0] if "<Code>" in body else ""
        raise S3Error(f"PUT {key}: HTTP {exc.code} {code}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise S3Error(f"PUT {key}: {getattr(exc, 'reason', exc)}") from exc


def upload_folder(conf, folder, prefix):
    """Every file of a backup folder → <prefix>/<folder name>/<relative path>. Returns s3://bucket/prefix/name."""
    name = os.path.basename(folder.rstrip("/"))
    base = "/".join(p for p in (prefix.strip("/"), name) if p)
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            path = os.path.join(root, f)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            with open(path, "rb") as fh:
                put_object(conf, f"{base}/{rel}", fh.read())
    return f"s3://{conf['bucket']}/{base}"


def conf_of(s):
    """The portal's S3 settings of PortalSettings s."""
    return {"endpoint": s.backup_s3_endpoint, "region": s.backup_s3_region or "us-east-1", "bucket": s.backup_s3_bucket,
            "access": s.backup_s3_access_key, "secret": s.get_secret("backup_s3_secret"), "style": s.backup_s3_style or "path"}


def conf_of_fe(s):
    """u06: the platforms' S3 settings, falling back to the portal's bucket when the platform ones are left empty."""
    if s.fe_backup_s3_endpoint and s.fe_backup_s3_bucket:
        return {"endpoint": s.fe_backup_s3_endpoint, "region": s.fe_backup_s3_region or "us-east-1",
                "bucket": s.fe_backup_s3_bucket, "access": s.fe_backup_s3_access_key,
                "secret": s.get_secret("fe_backup_s3_secret"), "style": s.fe_backup_s3_style or "path"}
    return conf_of(s)


def bucket_policy(bucket, prefix, principal="<the ARN of the portal's S3 user>"):
    """The bucket policy we recommend (Settings → Backups): the portal's user may only add backups under its prefix and
    read them back (restore); nobody deletes them; only HTTPS."""
    import json
    p = prefix.strip("/")
    objects = f"arn:aws:s3:::{bucket}/{p + '/' if p else ''}*"
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "PortalAddsBackups", "Effect": "Allow", "Principal": {"AWS": principal},
         "Action": ["s3:PutObject", "s3:GetObject"], "Resource": objects},
        {"Sid": "PortalListsItsBackups", "Effect": "Allow", "Principal": {"AWS": principal},
         "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}",
         "Condition": {"StringLike": {"s3:prefix": [f"{p}/*" if p else "*"]}}},
        {"Sid": "NoDeletes", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutLifecycleConfiguration",
                    "s3:PutBucketObjectLockConfiguration", "s3:BypassGovernanceRetention"],
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
         "Condition": {"StringEquals": {"aws:PrincipalArn": principal}}},
        {"Sid": "OnlyHTTPS", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
    ]}, indent=2)
