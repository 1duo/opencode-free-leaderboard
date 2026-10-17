"""Keep gated benchmark questions off routes without explicit no-training terms."""
from __future__ import annotations

import json
import re

from .config import CONTRIBUTOR_MODEL, CONTRIBUTOR_REASON


def privacy_evidence(soup, name: str) -> dict:
    statements = [li.get_text(' ', strip=True) for li in soup.find_all('li')
                  if re.match(re.escape(name) + r'(?=[:\s])', li.get_text(' ', strip=True))]
    no_training = (r'\b(?:does|do) not use your data for (?:model )?training\b|'
                   r'\b(?:prompts and other inputs|prompts|data) (?:are|is) not used for (?:model )?training\b')
    positive = bool(statements) and all(re.search(no_training, text, re.I) for text in statements)
    contradictory = any(re.search(r'may be used to improve|collect feedback and improve|improve NVIDIA products|permission to use',
                                  text, re.I) for text in statements)
    return {'gpqa_allowed': positive and not contradictory, 'privacy_statements': statements}


def current_proof(db, model: dict) -> dict:
    evidence = db.one('SELECT ok,evidence FROM discovery WHERE id=?', (model.get('evidence_id'),))
    if not evidence or not evidence['ok']:
        return {}
    return json.loads(evidence['evidence']).get('pricing', {}).get(model['id'], {})


def gpqa_allowed(db, model: dict) -> bool:
    if model['id'] == CONTRIBUTOR_MODEL:
        return False
    proof = current_proof(db, model)
    return proof.get('free') is True and proof.get('gpqa_allowed') is True


def public_only_reason(db, model: dict) -> str | None:
    if model['id'] == CONTRIBUTOR_MODEL:
        return CONTRIBUTOR_REASON
    proof = current_proof(db, model)
    if proof.get('free') is True and proof.get('gpqa_allowed') is not True:
        return 'GPQA withheld: current provider terms do not establish a no-training route.'
    return None
