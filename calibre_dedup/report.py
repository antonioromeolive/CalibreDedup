# Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
# Author: Antonio Romeo
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import csv
from pathlib import Path

from .models import Action, Plan

COLUMNS = ["source_id", "checked", "manual", "title", "authors", "action", "reason", "match", "add_formats",
           "unreadable_formats", "unreadable_to_trash", "publisher", "edition", "year", "isbn", "ai_used",
           "ai_fields", "status"]


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
                ",".join(it.add_formats), ",".join(sorted(it.bad_formats)),
                "yes" if it.bad_formats_to_trash else "", i.publisher or "", i.edition or "", i.year or "",
                ",".join(sorted(i.isbns)), "yes" if it.ai_used else "", ",".join(sorted(i.ai_fields)),
                it.status,
            ])
