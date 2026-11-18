#!/usr/bin/env python3
"""WS-AUTH probe: three real portal identities against the live evidence-review.

Asserts on CLAIMS AND BEHAVIOUR, never on a bare 200. Read-only: it lists, it
does not write. The tenant here is `datacompany` (the real one), so the probe is
deliberately confined to GETs — the separate `datacompany-probe` tenant exists for
anything that writes.
"""

import base64
import json
import subprocess
import sys
import urllib.parse
import urllib.request

TOKEN_URL = "https://auth.nonagentic.ai/realms/portal/protocol/openid-connect/token"
ERS = "http://127.0.0.1:8811"
CLIENT = "kag-frontend"  # fr-frontend has directAccessGrantsEnabled=false

LEAD = "georgiedekker+datacompany@gmail.com"
USERS = [LEAD, "nonagenticai+datacompany@gmail.com", "merqigtm+datacompany@gmail.com"]

FAIL = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAIL.append(label)


def password():
    out = subprocess.run(
        [
            "kubectl",
            "-n",
            "keycloak",
            "get",
            "secret",
            "keycloak-datacompany-reviewers",
            "-o",
            "jsonpath={.data.password}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return base64.b64decode(out).decode()


def grant(username, pw):
    body = urllib.parse.urlencode(
        {"grant_type": "password", "client_id": CLIENT, "username": username, "password": pw}
    ).encode()
    # Cloudflare fronts auth.nonagentic.ai and BANS urllib's default
    # `Python-urllib/3.x` User-Agent outright — error code 1010, which arrives as
    # an HTML block page, not an OIDC error. This is the same trap
    # `auth/verifier.py` already carries `JWKS_REQUEST_HEADERS` for; anything
    # scripting against this host needs a UA or it will read as "the account
    # cannot authenticate" when the account is fine.
    req = urllib.request.Request(
        TOKEN_URL, data=body, headers={"User-Agent": "datacompany-ws-auth-probe/1.0", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:200], "status": e.code}


def claims(token):
    p = token.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def get(path, token, extra=None):
    req = urllib.request.Request(f"{ERS}{path}", headers={"Authorization": f"Bearer {token}", **(extra or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    pw = password()
    toks, cl = {}, {}

    print("token grants")
    for u in USERS:
        d = grant(u, pw)
        ok = "access_token" in d
        check(f"{u} can obtain a token via {CLIENT}", ok, d.get("error", ""))
        if ok:
            toks[u] = d["access_token"]
            cl[u] = claims(d["access_token"])
    if len(toks) != 3:
        print("\nFAILED: not all three identities authenticate; nothing below is meaningful")
        return 1

    print("\nclaims")
    subs = {u: cl[u]["sub"] for u in USERS}
    check("all three subjects are DISTINCT", len(set(subs.values())) == 3, str(subs))

    tenants = {u: cl[u].get("tenant_id") for u in USERS}
    check("every token carries a tenant_id", all(tenants.values()), str(tenants))
    check("all three tenant_ids are BYTE-IDENTICAL", len(set(tenants.values())) == 1, str(tenants))
    t = next(iter(tenants.values()))
    check("the tenant_id is a uuid, not a legacy text tenant", len(t or "") == 36 and t.count("-") == 4, str(t))

    for u in USERS:
        roles = set(cl[u].get("realm_access", {}).get("roles", []))
        should = u == LEAD
        check(
            f"{'lead holds' if should else 'reviewer does NOT hold'} review-lead: {u.split('+')[0]}",
            ("review-lead" in roles) is should,
            str(sorted(roles)),
        )

    for u in USERS:
        present = [k for k in ("clientId", "clientHost", "clientAddress") if k in cl[u]]
        check(f"{u.split('+')[0]} is not a service principal", not present, str(present))

    print("\nevidence-review accepts each identity")
    for u in USERS:
        st, body = get("/api/reviews", toks[u])
        check(f"{u.split('+')[0]} -> GET /api/reviews", st == 200, f"HTTP {st} {str(body)[:120]}")

    print("\nnegative controls")
    st, _ = get("/api/reviews", "not-a-token")
    check("a garbage bearer is refused", st == 401, f"HTTP {st}")

    st, body = get("/api/reviews", toks[LEAD], {"X-Research-Context": f"Bearer {toks[LEAD]}"})
    check("a bearer-shaped X-Research-Context still resolves (precedence, not breakage)", st == 200, f"HTTP {st}")

    st, _ = get("/api/reviews", toks[LEAD], {"X-Tenant-Id": "0195f48b-d000-7000-8000-000000000102"})
    check("a mismatched X-Tenant-Id header does not 500", st == 200, f"HTTP {st}")

    print()
    if FAIL:
        print(f"FAILED ({len(FAIL)}): " + "; ".join(FAIL))
        return 1
    print(f"OK — three identities, one tenant {t}, lead={LEAD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
