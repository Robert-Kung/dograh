"""api.cli.ccp_first_tenant (mt-s1-foundation 2.5–2.6)."""

import pytest
from sqlalchemy import text

from api.cli import ccp_first_tenant as cli
from api.db.models import OrganizationModel


@pytest.fixture
def run(monkeypatch):
    """Run the CLI's coroutine on the test loop (asyncio.run would nest)."""

    async def _run(argv):
        captured = {}

        def fake_run(coro):
            captured["coro"] = coro
            return 0

        monkeypatch.setattr(cli.asyncio, "run", fake_run)
        cli.main(argv)
        return await captured["coro"]

    return _run


async def _orgs(async_session, n):
    await async_session.execute(text("DELETE FROM ccp_tenants"))
    out = []
    for i in range(n):
        org = OrganizationModel(provider_id=f"org_cli_secret_name_{i}")
        async_session.add(org)
        await async_session.flush()
        out.append(org.id)
    return out


async def test_check_tenant_and_non_tenant(db_session, async_session, run, capsys):
    a, b = await _orgs(async_session, 2)
    assert await run(["ensure", str(a)]) == 0
    assert await run(["check", str(a)]) == 0
    assert await run(["check", str(b)]) == 1
    out = capsys.readouterr().out
    assert f"organization {a}: active" in out
    assert "secret_name" not in out


async def test_ensure_refuses_when_not_empty(db_session, async_session, run, capsys):
    a, b = await _orgs(async_session, 2)
    assert await run(["ensure", str(a)]) == 0
    assert await run(["ensure", str(b)]) == 1
    assert "not empty" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["0", "-1", "1; id", "abc"])
def test_rejects_bad_id(bad):
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", bad])
    assert exc.value.code != 0
