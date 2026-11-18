def test_bucket_manifest_path_is_accepted(frappe):
    from saas_core.provisioning import templates
    src = {"type": "bucket", "bucket": "b", "key": "golden/x.sql.gz", "manifest": "golden/manifest.json"}
    # a path-style manifest is resolved and verified at restore time, so validation must not reject it
    try:
        templates.validate_template({"slug": "t", "backup_source": src, "site_config": {}, "sanitize": {}})
    except templates.TemplateError as e:
        assert "manifest" not in str(e)
