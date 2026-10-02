"""Run the fixed, labeled email relevance set against the production classifier."""
import hashlib
import json
import math
from pathlib import Path

import mail_agent


DATA = Path("eval_mail/relevance_v1.json")
OUT = Path("eval_mail/relevance_v1-results.json")


def wilson(hits, total):
    if not total:
        return None
    z = 1.96
    p = hits / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / d
    return [round(center - margin, 4), round(center + margin, 4)]


def main():
    raw = DATA.read_bytes()
    cases = json.loads(raw)
    rows = []
    for case in cases:
        history = [{"role": "user", "content": text} for text in case.get("prior", [])]
        try:
            predicted = mail_agent.mail_related(case["current"], history)
            error = None
        except Exception as exc:
            predicted, error = None, f"{type(exc).__name__}: {exc}"
        rows.append({**case, "predicted": predicted, "error": error})
        print(f"{case['id']} expected={case['label']} predicted={predicted} {error or ''}", flush=True)
    tp = sum(r["label"] and r["predicted"] is True for r in rows)
    fn = sum(r["label"] and r["predicted"] is False for r in rows)
    fp = sum(not r["label"] and r["predicted"] is True for r in rows)
    tn = sum(not r["label"] and r["predicted"] is False for r in rows)
    errors = sum(r["predicted"] is None for r in rows)
    ratio = lambda a, b: round(a / b, 4) if b else None
    result = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "classifier_sha256": hashlib.sha256(Path("mail_agent.py").read_bytes()).hexdigest(),
        "counts": {"tp": tp, "fn": fn, "fp": fp, "tn": tn, "errors": errors},
        "metrics": {
            "accuracy": ratio(tp + tn, len(rows)),
            "accuracy_wilson_95": wilson(tp + tn, len(rows)),
            "precision": ratio(tp, tp + fp),
            "recall": ratio(tp, tp + fn + sum(r["label"] and r["predicted"] is None for r in rows)),
            "specificity": ratio(tn, tn + fp + sum(not r["label"] and r["predicted"] is None for r in rows)),
            "f1": ratio(2 * tp, 2 * tp + fp + fn),
            "balanced_accuracy": ratio(
                ratio(tp, tp + fn + sum(r["label"] and r["predicted"] is None for r in rows))
                + ratio(tn, tn + fp + sum(not r["label"] and r["predicted"] is None for r in rows)), 2),
        },
        "rows": rows,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": result["counts"], "metrics": result["metrics"]}, indent=2))


if __name__ == "__main__":
    assert wilson(0, 0) is None and wilson(1, 1)[1] == 1.0
    main()
