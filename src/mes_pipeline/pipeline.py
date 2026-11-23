"""End-to-end orchestration: extract -> validate -> transform -> load."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .extract import extract
from .load import connect, load
from .transform import build_facts

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    run_id: str
    run_log: pd.DataFrame
    table_rows: dict[str, int]
    duplicates_removed: int


def run_pipeline(raw_dir: Path, db_path: Path | str) -> RunSummary:
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    log.info("run %s: extracting from %s", run_id, raw_dir)

    batch = extract(raw_dir)
    for res in (batch.events, batch.counts, batch.sensors):
        log.info("  %-24s read=%7d valid=%7d rejected=%5d", res.source, res.rows_read, len(res.valid), len(res.rejects))

    facts = build_facts(batch.events.valid, batch.counts.valid, batch.sensors.valid)
    log.info("  removed %d duplicate events", facts.duplicates_removed)

    finished = datetime.now(timezone.utc).replace(tzinfo=None)
    run_log = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "started_at": started,
                "finished_at": finished,
                "source": res.source,
                "rows_read": res.rows_read,
                "rows_valid": len(res.valid),
                "rows_rejected": len(res.rejects),
            }
            for res in (batch.events, batch.counts, batch.sensors)
        ]
    )
    con = connect(db_path)
    try:
        rows = load(con, facts, batch.rejects, run_log)
    finally:
        con.close()
    log.info("run %s: loaded %s into %s", run_id, rows, db_path)
    return RunSummary(run_id, run_log, rows, facts.duplicates_removed)
