import hashlib
import pytest


def src(blobs, prefix="golden/acme-v16-in/20260101_000000"):
    return {"bucket": "acme-backups", "prefix": prefix, "manifest": {"parts": [
        {"name": n, "sha256": hashlib.sha256(b).hexdigest()} for n, b in blobs.items()]}}


BLOBS = {"database.sql.gz": b"db", "public.tar": b"pub", "private.tar": b"priv"}


def put(port, s, blobs=BLOBS):
    for n, b in blobs.items():
        port.bucket_put(s["bucket"], f"{s['prefix']}/{n}", b)


def test_bucket_restore_via_upload_path(frappe):
    from saas_core.provisioning.backups import restore_bucket_source
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    port = FakeFcctl(); s = src(BLOBS); put(port, s)
    assert restore_bucket_source(port, "a.test", s, workflow_id="w1").ok
    order = [c["method"] for c in port.calls if c["method"] != "bucket_get"]
    assert order == ["bucket_put"] * 3 + ["create_upload"] + ["put_upload_part"] * 3 + ["restore_from_upload"]
    assert port.count("bucket_get") == 3
    r = port.restores[0]
    assert r["parts"] == list(BLOBS) and r["upload_id"] == "up-1"


def test_sha_mismatch_aborts_before_upload(frappe):
    from saas_core.provisioning.backups import BackupError, restore_bucket_source
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    port = FakeFcctl(); s = src(BLOBS); put(port, s, {**BLOBS, "public.tar": b"tampered"})
    with pytest.raises(BackupError, match="sha256"):
        restore_bucket_source(port, "a.test", s)
    assert port.count("create_upload") == 0


def test_template_validates_bucket_source(frappe):
    from saas_core.provisioning.templates import TemplateError, validate_template
    doc = {"slug": "t", "apps": [{"app": "frappe"}], "backup_source": src(BLOBS)}
    assert validate_template(doc)["backup_source"]["bucket"] == "acme-backups"
    doc["backup_source"] = {"bucket": "b", "prefix": "x"}
    with pytest.raises(TemplateError):
        validate_template(doc)
