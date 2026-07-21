from clinic_voice.deploy import cliniko_catalog_ready


def test_seeded_catalog_is_ready_for_deployment_bootstrap():
    assert cliniko_catalog_ready() is True
