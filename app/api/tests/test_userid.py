from ai4ia_api.auth.userid import internal_user_id


def test_internal_user_id_is_deterministic():
    a = internal_user_id(provider="entra", issuer="iss", subject="oid-1", tenant_id="t1")
    b = internal_user_id(provider="entra", issuer="iss", subject="oid-1", tenant_id="t1")
    assert a == b


def test_internal_user_id_varies_by_tenant_and_provider():
    base = dict(issuer="iss", subject="oid-1")
    assert internal_user_id(provider="entra", tenant_id="t1", **base) != internal_user_id(
        provider="entra", tenant_id="t2", **base
    )
    assert internal_user_id(provider="entra", tenant_id="t1", **base) != internal_user_id(
        provider="dev", tenant_id="t1", **base
    )
