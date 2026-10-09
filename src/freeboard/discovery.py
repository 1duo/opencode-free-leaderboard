from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .budget import Budget
from .config import OMITTED_MODELS, ZEN_BASE, ZEN_DOCS, credential, digest, now, week
from .db import DB


def parse_evidence(html: str) -> dict[str, dict]:
    """Exact table joins; never infer eligibility from a free suffix."""
    soup = BeautifulSoup(html, "html.parser")
    endpoints: dict[str, dict] = {}
    prices: dict[str, list[list[str]]] = {}
    for table in soup.find_all("table"):
        rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                for row in table.find_all("tr")]
        if not rows:
            continue
        header = [c.lower() for c in rows[0]]
        if "model id" in header and "endpoint" in header:
            for row in rows[1:]:
                if len(row) < 3:
                    continue
                ident = row[header.index("model id")].strip("` ")
                endpoint = row[header.index("endpoint")].strip("` ")
                endpoints[ident] = {"name": row[0], "endpoint": endpoint}
        if "input" in header and "output" in header and "model" in header:
            for row in rows[1:]:
                if len(row) >= 3:
                    prices.setdefault(row[0], []).append(row[1:])
    result = {}
    for ident, entry in endpoints.items():
        evidence = prices.get(entry["name"], [])
        # '-' denotes an inapplicable cache operation. Input and output must be explicit.
        free = bool(evidence) and all(len(p) >= 2 and p[0].lower() == "free"
                                     and p[1].lower() == "free"
                                     and all(x.lower() in {"free", "-", "—"} for x in p)
                                     for p in evidence)
        result[ident] = {**entry, "prices": evidence, "free": free}
    return result


def profile_for(endpoint: str | None, reasoning: dict | None = None) -> dict:
    from .opencode import profile
    path = urlparse(endpoint or "").path
    if path.endswith("/chat/completions"):
        return profile('chat', reasoning)
    if path.endswith("/responses"):
        return profile('responses', reasoning)
    return {"protocol": "unsupported", "cap_verified": False}


def excluded(ident: str) -> bool:
    return ident in OMITTED_MODELS or ("muse" in ident.lower() and "contributor" in ident.lower())


def discover(db: DB, budget: Budget, client: httpx.Client | None = None) -> dict:
    from .opencode import client_version, inspect_models, reasoning_policy
    client_version(db.state)
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    content = {}
    error = None
    for label, url in [("pricing", ZEN_DOCS), ("catalog", f"{ZEN_BASE}/models")]:
        attempt = budget.reserve("discovery")
        try:
            key = credential("zen") if label == "catalog" else None
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            response = client.get(url, headers=headers)
            response.raise_for_status()
            content[label] = response.text
            budget.finish(attempt, "received", response.status_code)
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            budget.finish(attempt, "failed", status)
            error = f"{label} unavailable" + (f" (HTTP {status})" if status else "")
    evidence = parse_evidence(content.get("pricing", ""))
    try:
        catalog = json.loads(content.get("catalog", "{}"))
        ids = {m["id"] for m in catalog["data"] if isinstance(m.get("id"), str)}
        if not ids or not evidence:
            raise ValueError("Empty or unrecognized official source")
        ok = error is None
    except (ValueError, KeyError, TypeError):
        ok, ids = False, set()
        error = error or "Official discovery schema is unavailable or changed"
    free_ids = sorted(i for i in ids if not excluded(i) and evidence.get(i, {}).get('free')
                      and profile_for(evidence[i]['endpoint'])['protocol'] == 'opencode') if ok else []
    try:
        native_models = inspect_models(budget.settings, free_ids) if free_ids else {}
    except (RuntimeError, httpx.HTTPError, ValueError, KeyError, TypeError):
        ok, native_models = False, {}
        error = 'Native OpenCode model metadata unavailable'
    stamp = now()
    raw = {"sources": [ZEN_DOCS, f"{ZEN_BASE}/models"], "pricing": evidence,
           "catalog_ids": sorted(ids), "hashes": {k: digest(v) for k, v in content.items()}}
    ident = db.execute("INSERT INTO discovery(observed_at,week,ok,evidence,error) VALUES(?,?,?,?,?)",
                       (stamp, week(), int(ok), json.dumps(raw), error)).lastrowid
    previous = {m["id"]: m for m in db.rows("SELECT * FROM models")}
    candidates = ids | set(previous) | {k for k, v in evidence.items() if v["free"]}
    for model in sorted(candidates):
        old, proof = previous.get(model), evidence.get(model)
        endpoint = proof["endpoint"] if proof else (old or {}).get("endpoint")
        try:
            native = native_models.get(model)
            if native and (native['api']['id'] != model or native['api']['url'] != ZEN_BASE
                           or native['providerID'] != 'opencode' or native['cost']['input'] != 0 or native['cost']['output'] != 0):
                raise ValueError('Native route or zero pricing is inconsistent')
            reasoning = reasoning_policy(native)
        except (ValueError, KeyError, TypeError):
            reasoning = reasoning_policy(None)
        default = profile_for(endpoint, reasoning)
        prior = json.loads(old['profile']) if old else {}
        same = (old and old['endpoint'] == endpoint and
                {k: v for k, v in prior.items() if k != 'cap_verified'} ==
                {k: v for k, v in default.items() if k != 'cap_verified'})
        profile = prior if same else default
        if excluded(model):
            status = "excluded"
        elif not ok:
            status = "verification_unavailable"
        elif model not in ids:
            status = "removed"
        elif not proof or not proof["prices"]:
            status = "pricing_unknown"
        elif not proof["free"]:
            status = "paid"
        elif not endpoint or not endpoint.startswith(ZEN_BASE + "/"):
            status = "unsupported"
        elif profile["protocol"] == "unsupported":
            status = "unsupported"
        elif profile.get('reasoning', {}).get('mode') == 'unverified':
            status = 'configuration_error'
        else:
            status = "eligible"
        if status == 'eligible' and same and old['status'] in {'cap_violation', 'cap_unverified'}:
            status = old['status']
        epoch = digest({"id": model, "endpoint": endpoint,
                        "profile": {k: v for k, v in profile.items() if k != "cap_verified"}})[:16]
        db.execute("""INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,endpoint=excluded.endpoint,protocol=excluded.protocol,
            status=excluded.status,epoch=excluded.epoch,profile=excluded.profile,
            observed_at=excluded.observed_at,evidence_id=excluded.evidence_id""",
                   (model, (proof or {}).get("name", (old or {}).get("name", model)), endpoint,
                    profile["protocol"], status, epoch, int(digest(model)[:8], 16) % 4,
                    json.dumps(profile), stamp, ident))
        db.execute("INSERT INTO observations(discovery_id,model_id,epoch,status,evidence) VALUES(?,?,?,?,?)",
                   (ident, model, epoch, status, json.dumps(proof or {})))
    return {"ok": ok, "observed_at": stamp, "error": error,
            "eligible": len(db.rows("SELECT id FROM models WHERE status='eligible'"))}
