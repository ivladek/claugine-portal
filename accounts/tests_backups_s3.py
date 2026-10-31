"""u04 (owner 2026-10-31): backups to an S3 bucket (the portal's and the platforms' front ends), and the front ends'
backups that replace claugine-cli jobs/fe-backup.sh."""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import fe_backup, s3
from tenants.models import PortalSettings

AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class S3Signing(TestCase):
    def test_aws_example(self):
        # the "GET Object" example of the AWS Signature Version 4 documentation
        h = s3.sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {"range": "bytes=0-9"}, s3.EMPTY_SHA256,
                    AWS_KEY, AWS_SECRET, "us-east-1", when=datetime(2013, 5, 24, tzinfo=dt_timezone.utc))
        self.assertTrue(h["authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_upload_folder(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        b = os.path.join(d, "claugine-portal-20261031-1200")
        os.makedirs(b)
        for n in ("database.sql.gz", "MANIFEST"):
            with open(os.path.join(b, n), "wb") as fh:
                fh.write(b"x" * 10)
        sent = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=0):
            sent.append(req)
            return Resp()
        conf = {"endpoint": "https://s3.example.com", "region": "kz-1", "bucket": "bk", "access": AWS_KEY,
                "secret": AWS_SECRET, "style": "path"}
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            where = s3.upload_folder(conf, b, "claugine-portal")
        self.assertEqual(where, "s3://bk/claugine-portal/claugine-portal-20261031-1200")
        self.assertEqual(sorted(r.full_url for r in sent), [
            "https://s3.example.com/bk/claugine-portal/claugine-portal-20261031-1200/MANIFEST",
            "https://s3.example.com/bk/claugine-portal/claugine-portal-20261031-1200/database.sql.gz"])
        self.assertTrue(all(r.get_method() == "PUT" and r.get_header("Content-md5") for r in sent))
        self.assertIn("kz-1/s3/aws4_request", sent[0].get_header("Authorization"))
        pol = json.loads(s3.bucket_policy("bk", "claugine-portal"))
        self.assertIn("arn:aws:s3:::bk/claugine-portal/*", json.dumps(pol))


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class FrontEndBackups(TestCase):
    def setUp(self):
        from inventory.models import Platform, Site
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        s = PortalSettings.get()
        s.fe_backup_path, s.fe_backup = self.dir, True  # u06: the platforms' own local folder and password
        s.set_secret("backup_password", "Archive-Pass-2026")
        s.set_secret("fe_backup_password", "Archive-Pass-2026")
        s.save()
        site = Site.objects.create(code="dc1")
        self.p = Platform.objects.create(key="dc1.cloud", site=site, name="cloud", kind="shared", fe_vip="10.0.0.30",
                                         fe_secrets="secrets.dc1.one", fe_mode="primary")

    def fake_fetch(self, host, port, user, key, remote, out, timeout=0):
        self.calls.append((host, remote))
        with open(out, "wb") as fh:
            fh.write(b"data of " + host.encode())
        return ""

    def test_run(self):
        self.calls = []
        nodes = (0, "MASTER", [("2", "one3", "10.0.0.33", "leader"), ("0", "one1", "10.0.0.31", "follower")])
        sec = mock.Mock(**{"has.return_value": True, "get.return_value": "KEY"})
        with mock.patch("accounts.fe_backup._nodes", return_value=nodes), \
                mock.patch("inventory.credentials.find", return_value=sec), \
                mock.patch("inventory.sshexec.fetch", side_effect=self.fake_fetch):
            result = fe_backup.run()
        self.assertTrue(result.startswith("ok: 1 platforms"), result)
        folder = os.path.join(self.dir, "platforms")
        [b] = os.listdir(folder)
        self.assertTrue(b.startswith("platform-dc1.cloud_10.0.0.30_"))
        files = sorted(os.listdir(os.path.join(folder, b)))
        self.assertEqual(files, ["0-one1-10.0.0.31_follower.zip", "2-one3-10.0.0.33_leader.zip", "MANIFEST"])
        import pyzipper
        with pyzipper.AESZipFile(os.path.join(folder, b, "2-one3-10.0.0.33_leader.zip")) as zf:
            zf.setpassword(b"Archive-Pass-2026")
            self.assertEqual(sorted(zf.namelist()), ["config.tar.gz", "onedb-federated.sql.gz", "onedb-zone.sql.gz"])
        self.assertTrue(any("onedb backup" in r and "--federated" in r for _h, r in self.calls))
        self.assertFalse(any("onedb" in r for h, r in self.calls if h == "10.0.0.31"))  # the database from the leader only
        self.assertIsNone(PortalSettings.get().fe_backup_requested_at)

    def test_settings(self):
        from accounts.tests_ha import staff, otp
        sa = staff(is_super_admin=True)
        self.client.force_login(sa)
        otp(self.client)
        url = reverse("portal_settings")
        r = self.client.get(url + "?tab=backups")
        self.assertContains(r, "Platform front ends")
        self.assertContains(r, "dc1.cloud")
        self.client.post(url, {"tab": "backups", "section": "fe_backup", "fe_backup": "on", "fe_backup_time": "04:10",
                               "fe_backup_keep_days": "7", "fe_backup_period_hours": "24", "fe_backup_path": self.dir,
                               "fe_backup_password": "Plat-Pass-2026", "fe_backup_dirs": "/etc/one\n/var/lib/one/.ssh"})
        s = PortalSettings.get()
        self.assertEqual((s.fe_backup_time.hour, s.fe_backup_keep_days, fe_backup.dirs(s)), (4, 7, ["/etc/one", "/var/lib/one/.ssh"]))
        self.assertTrue(s.has_secret("fe_backup_password"))
        self.client.post(url, {"tab": "backups", "section": "fe_backup_now"})
        self.assertIsNotNone(PortalSettings.get().fe_backup_requested_at)
        self.assertTrue(fe_backup.due(PortalSettings.get()))


@override_settings(FIELD_ENCRYPTION_KEY="m3Jb0bC1k0qWw8bq3cJmHqk6kVqZ0b9mJxS6m0f4n1A=")
class PortalBackupToS3(TestCase):
    def test_local_and_s3(self):
        from accounts import appliance
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        s = PortalSettings.get()
        s.backup_path, s.backup_s3, s.backup_s3_bucket, s.backup_s3_endpoint = d, True, "bk", "https://s3.example.com"
        s.set_secret("backup_password", "Archive-Pass-2026")
        s.set_secret("backup_s3_secret", AWS_SECRET)
        s.save()

        def dump(tmp):
            out = os.path.join(tmp, "database.sql")
            open(out, "w").close()
            return out
        p = mock.patch("accounts.appliance._dump", side_effect=dump)
        p.start()
        self.addCleanup(p.stop)
        with mock.patch("accounts.s3.upload_folder", return_value="s3://bk/claugine-portal/x") as up:
            r = appliance.backup(PortalSettings.get())
        self.assertTrue(r.startswith("ok: ") and "s3://bk/claugine-portal/x" in r, r)
        self.assertEqual(up.call_args.args[0]["secret"], AWS_SECRET)
        with mock.patch("accounts.s3.upload_folder", side_effect=s3.S3Error("PUT x: HTTP 403 AccessDenied")):
            r = appliance.backup(PortalSettings.get())
        self.assertTrue(r.startswith("failed:") and "AccessDenied" in r and "kept in" in r, r)
        PortalSettings.objects.update(backup_path="")  # u05: S3 only (no local folder)
        with mock.patch("accounts.s3.upload_folder", return_value="s3://bk/claugine-portal/y"):
            r = appliance.backup(PortalSettings.get())
        self.assertTrue(r.startswith("ok: s3://bk/claugine-portal/y"), r)
