"""registry-refresh: the NIGHTLY refresh of the children registry (cron). READ-ONLY on Famly and HubSpot; it writes
only to the registry (var/, gitignored), through the backfill (backfill.py), which only ever ADDS links and
refreshes names, dates of birth and sites: a URN or deal already tied to another child is reported, never
overwritten, and nothing is ever deleted.

Guards (nothing is written, and one failure line goes to Slack):
  * a Famly or HubSpot read fails (the backfill raises);
  * Famly returns too few children: fewer than MIN_SHARE of the registry's children in total, or, for a site with
    at least SITE_MIN registry children, fewer than SITE_SHARE of them (a partial roster must never be taken as
    the truth).

Drift: a registry child that Famly no longer has (merged or deleted in the UI) is flagged `registry_child_gone`;
its row stays (a person decides).

Alerts like onboard-quality-check: the issues (ALERT_CODES, as code + child id + deal id) are diffed with the last
run's state (registry_refresh_state.json beside the registry). The first run posts a one-line baseline; later runs
post only NEW issues; a quiet night posts nothing. A post that fails leaves the state unchanged, so the same issues
are new again next night. Slack and the CSV carry codes and ids only, never a name.
"""

import csv
import json
import logging
from datetime import date
from pathlib import Path

from . import backfill, store

logger = logging.getLogger(__name__)

MIN_SHARE = 0.9
SITE_SHARE = 0.5
SITE_MIN = 10
CHILD_GONE = "registry_child_gone"
REPORTS_DIR = "reports"
KEEP_REPORTS = 14
MAX_IDS = 10
STATE_FILE_NAME = "registry_refresh_state.json"

# The codes worth a person's attention. Others (deal_no_child_found, child_without_dob,
# reference_not_operations_or_adhoc) stay in the CSV only.
ALERT_CODES = {
    backfill.DEAL_SEVERAL_CHILDREN: "A deal points at more than one Famly child; it is not linked in the registry.",
    backfill.URN_SEVERAL_CHILDREN: "A URN is on more than one Famly child.",
    backfill.STORE_CONFLICT: "Famly or HubSpot now ties a URN or deal to a different child than the registry; the "
                             "registry was not changed.",
    backfill.RECORDED_CHILD_MISSING: "A deal or job points at a Famly child that does not exist.",
    CHILD_GONE: "A child in the registry is no longer in Famly (merged or deleted); the registry row was kept.",
}


class RefreshAborted(Exception):
    """The refresh stopped before writing anything. `code` is a short code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def state_path() -> Path:
    return store.store_path().parent / STATE_FILE_NAME


def load_state(path: Path) -> set | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {tuple(item) for item in data["issues"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_state(path: Path, issues: set, today: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runDate": today.isoformat(), "issues": sorted(list(i) for i in issues)},
                               indent=1), encoding="utf-8")


def check_counts(registry_sites: dict, famly_children: dict, site_codes: dict) -> str | None:
    """None when Famly's roster looks complete, else the abort code."""
    total = len(registry_sites)
    if total and len(famly_children) < MIN_SHARE * total:
        return "famly_roster_too_small"
    famly_by_site: dict = {}
    for child in famly_children.values():
        code = backfill._site_code(child, site_codes)
        famly_by_site[code] = famly_by_site.get(code, 0) + 1
    registry_by_site: dict = {}
    for code in registry_sites.values():
        registry_by_site[code] = registry_by_site.get(code, 0) + 1
    for code, count in registry_by_site.items():
        if code and count >= SITE_MIN and famly_by_site.get(code, 0) < SITE_SHARE * count:
            return "famly_site_roster_too_small"
    return None


def _post(post, text: str) -> bool:
    try:
        return bool(post(text))
    except Exception as exc:  # noqa: BLE001 - Slack must never fail the run
        logger.warning("registry-refresh: Slack post failed (%s)", type(exc).__name__)
        return False


def _ref(child_id: str, deal_id: str) -> str:
    from actions.onboard import notifier
    parts = ([f"child {child_id}"] if child_id else []) + ([f"deal {notifier.deal_ref(deal_id)}"] if deal_id else [])
    return " / ".join(parts) or "-"


def new_issues_text(new: set, open_count: int) -> str:
    lines = [f"*Children registry: {len(new)} new issue(s)* ({open_count} open in all; ids only, details in "
             f"reports/)"]
    by_code: dict = {}
    for code, child_id, deal_id in sorted(new):
        by_code.setdefault(code, []).append(_ref(child_id, deal_id))
    for code, refs in sorted(by_code.items()):
        shown = ", ".join(refs[:MAX_IDS]) + (f" and {len(refs) - MAX_IDS} more" if len(refs) > MAX_IDS else "")
        lines.append(f"• {ALERT_CODES[code]} ({len(refs)}): {shown}")
    return "\n".join(lines)


def write_report(rows, reports_dir: Path, today: date) -> Path:
    """reports/registry_refresh_YYYY-MM-DD.csv (codes and ids only), keeping the last KEEP_REPORTS."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"registry_refresh_{today.isoformat()}.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "famly_child_id", "deal_id"])
        writer.writerows(rows)
    for old in sorted(reports_dir.glob("registry_refresh_2*.csv"), reverse=True)[KEEP_REPORTS:]:
        old.unlink()
    return target


def run(hubspot, gql, *, dry_run: bool = False, post=None, today: date | None = None,
        state_file: Path | None = None, reports_dir: Path | str | None = None, site_ids=None,
        children=None, links=None, site_codes=None) -> dict:
    """The nightly refresh. Returns counts only. Raises RefreshAborted (after one Slack line) when it stopped
    before writing; a read failure is reported the same way and re-raised. `dry_run`: no registry write, no
    state, no Slack."""
    from actions.onboard import notifier
    from actions.onboard_audit import runner as audit

    today = today or date.today()
    post = post or notifier.SlackNotifier().notice
    state_file = state_file or state_path()
    reports_dir = Path(reports_dir or REPORTS_DIR)
    site_codes = backfill._site_codes() if site_codes is None else site_codes

    def fail(code: str):
        if not dry_run:
            _post(post, f"⚠️ Children registry refresh stopped ({code}). Nothing was written; it runs again "
                        "tomorrow. Ask an admin if this repeats.")
        logger.error("registry-refresh: stopped code=%s", code)
        return RefreshAborted(code)

    try:
        before = store.child_ids_by_site()
        if children is None:
            if site_ids is None:
                from actions.urn_lookup import runner as urn_runner
                site_ids = urn_runner._all_institution_ids()
            children = audit.fetch_children(gql, site_ids)
    except Exception as exc:  # noqa: BLE001 - its type only
        raise fail(f"read_failed_{type(exc).__name__}") from None
    code = check_counts(before, children, site_codes)
    if code:
        raise fail(code)
    try:
        summary, report = backfill.run(hubspot, gql, confirm=not dry_run, children=children, links=links,
                                       site_codes=site_codes)
    except Exception as exc:  # noqa: BLE001
        raise fail(f"read_failed_{type(exc).__name__}") from None
    no_child = summary.pop("_no_child_deals")

    for child_id in sorted(set(before) - set(children)):
        report.add(CHILD_GONE, child_id)
    issues = {(c, child, deal) for c, child, deal in report.rows if c in ALERT_CODES}
    target = write_report(report.rows, reports_dir, today)
    backfill.write_deal_list(str(reports_dir / "registry_deals_without_child.txt"), no_child)

    result = {"mode": "dry_run" if dry_run else "refresh", "famlyChildren": len(children),
              "registryBefore": len(before), "childrenGone": report.counts[CHILD_GONE],
              "openIssues": len(issues), "codes": dict(sorted(report.counts.items())),
              "written": summary.get("written"), "liveDealsWithoutChild": len(no_child), "report": str(target)}
    if dry_run:
        return result

    previous = load_state(state_file)
    new = issues - (previous or set())
    posted, has_post = False, previous is None or bool(new)
    if previous is None:
        posted = _post(post, f"Children registry refresh: first run. {len(children)} Famly children, "
                             f"{len(issues)} open issue(s) recorded as the baseline (ids in reports/).")
    elif new:
        posted = _post(post, new_issues_text(new, len(issues)))
    post_failed = has_post and not posted
    if not post_failed:
        save_state(state_file, issues, today)
    logger.info("registry-refresh: children=%d gone=%d open=%d new=%d posted=%s", len(children),
                report.counts[CHILD_GONE], len(issues), len(new), posted)
    result.update({"new": len(new), "resolved": len((previous or set()) - issues), "posted": posted,
                   "postFailed": post_failed})
    return result
