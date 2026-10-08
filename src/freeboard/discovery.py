from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .budget import Budget
from .config import ZEN_BASE, ZEN_DOCS, credential, digest, now, week
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


def profile_for(endpoint: str | None) -> dict:
    path = urlparse(endpoint or "").path
    if path.endswith("/chat/completions"):
        return {"protocol": "chat", "cap_parameter": "max_completion_tokens",
                "temperature": 0, "cap_verified": False}
    if path.endswith("/responses"):
        return {"protocol": "responses", "cap_parameter": "max_output_tokens",
                "temperature": 0, "cap_verified": False}
    return {"protocol": "unsupported", "cap_verified": False}


def excluded(ident: str) -> bool:
    return "muse" in ident.lower() and "contributor" in ident.lower()


def discover(db: DB, budget: Budget, client: httpx.Client | None = None) -> dict:
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
        default = profile_for(endpoint)
        profile = json.loads(old["profile"]) if old and old["endpoint"] == endpoint else default
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
        else:
            status = "eligible"
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

