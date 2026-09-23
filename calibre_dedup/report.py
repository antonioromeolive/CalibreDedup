from __future__ import annotations

import csv
from pathlib import Path

from .models import Action, Plan

COLUMNS = ["source_id", "checked", "manual", "title", "authors", "action", "reason", "match", "add_formats",
           "publisher", "edition", "year", "isbn", "ai_used", "ai_fields", "status"]


def write_csv(plan: Plan, path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for it in plan.items:
            i = it.identity
            w.writerow([
                it.source.id, "yes" if it.selected and it.action is not Action.LEAVE else "",
                "yes" if it.manual else "", i.title or it.source.title, " & ".join(i.authors or it.source.authors),
                it.action.value, it.reason, it.match.label() if it.match else "",
                ",".join(it.add_formats), i.publisher or "", i.edition or "", i.year or "",
                ",".join(sorted(i.isbns)), "yes" if it.ai_used else "", ",".join(sorted(i.ai_fields)),
                it.status,
            ])
