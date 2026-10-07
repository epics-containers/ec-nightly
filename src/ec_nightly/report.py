"""Write run results as JSON and JUnit XML into REPORT_DIR."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .checks import FAIL, SKIP, Result
from .config import Config


def write_reports(
    config: Config,
    results: list[Result],
    started: datetime,
    version: str,
) -> Path:
    """Write ``<REPORT_DIR>/<timestamp>/<JOB_NAME>/report.{json,xml}``."""
    out = Path(config.report_dir) / started.strftime("%Y-%m-%dT%H%M%SZ")
    out = out / config.job_name
    out.mkdir(parents=True, exist_ok=True)
    failures = sum(r.status == FAIL for r in results)
    summary = {
        "tool": "ec-nightly",
        "version": version,
        "job_name": config.job_name,
        "instrument": config.instrument,
        "blueapi_url": config.blueapi_url,
        "started": started.isoformat(),
        "passed": failures == 0,
        "counts": {
            status: sum(r.status == status for r in results)
            for status in ("pass", FAIL, SKIP)
        },
        "results": [asdict(r) for r in results],
    }
    (out / "report.json").write_text(json.dumps(summary, indent=2) + "\n")

    suite = ET.Element(
        "testsuite",
        name=f"ec-nightly.{config.instrument or config.job_name}",
        tests=str(len(results)),
        failures=str(failures),
        skipped=str(sum(r.status == SKIP for r in results)),
        time=f"{sum(r.duration for r in results):.3f}",
        timestamp=started.isoformat(),
    )
    for r in results:
        case = ET.SubElement(
            suite, "testcase", classname="ec_nightly", name=r.name, time=str(r.duration)
        )
        if r.status == FAIL:
            ET.SubElement(case, "failure", message=r.message)
        elif r.status == SKIP:
            ET.SubElement(case, "skipped", message=r.message)
    ET.ElementTree(suite).write(
        out / "report.xml", encoding="utf-8", xml_declaration=True
    )
    return out
