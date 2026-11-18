def test_restore_fetches_path_manifest_from_bucket(frappe):
    from saas_core.provisioning.fake_fcctl import FakeFcctl
    from saas_core.provisioning import backups
    fake = FakeFcctl()
    src = {"type": "bucket", "bucket": "acme-backups", "key": "golden/x/db.sql.gz", "manifest": "golden/x/manifest.json"}
    res = backups.restore_bucket_source(fake, "s1.example", src, workflow_id="wf-1")
    assert res is not None
    assert ("acme-backups", "golden/x/manifest.json") in fake.bucket
