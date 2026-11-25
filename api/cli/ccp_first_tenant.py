"""Thin CLI around api.services.ccp.tenants for the first tenant (mt-s1-foundation).

    python -m api.cli.ccp_first_tenant check  <organization_id>
    python -m api.cli.ccp_first_tenant ensure <organization_id>

``check`` exits 0 iff the organization is a CCP tenant; ``ensure`` makes it the
active first tenant iff ccp_tenants is empty (RUNBOOK repair step). Output is
the organization id and status only.
"""

import argparse
import asyncio
import sys

from api.services.ccp import tenants


def _org_id(raw: str) -> int:
    if not raw.isdigit() or not 1 <= int(raw) <= 2147483647:
        raise argparse.ArgumentTypeError("organization_id must be a positive integer")
    return int(raw)


async def _check(org_id: int) -> int:
    tenant = await tenants.get_tenant(org_id)
    if tenant is None:
        print(f"organization {org_id}: not a CCP tenant")
        return 1
    print(f"organization {org_id}: {tenant.status}")
    return 0


async def _ensure(org_id: int) -> int:
    try:
        created = await tenants.ensure_first_tenant(org_id)
    except tenants.OrganizationNotFound:
        print(f"organization {org_id}: not found — nothing created")
        return 1
    if created:
        print(f"organization {org_id}: created as active first tenant")
        return 0
    print(
        f"organization {org_id}: not created — ccp_tenants is not empty; "
        "the first tenant already exists (see RUNBOOK)"
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccp_first_tenant")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "ensure"):
        sub.add_parser(name).add_argument("organization_id", type=_org_id)
    args = parser.parse_args(argv)
    run = _check if args.cmd == "check" else _ensure
    return asyncio.run(run(args.organization_id))


if __name__ == "__main__":
    sys.exit(main())
