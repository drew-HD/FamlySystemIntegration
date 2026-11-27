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

The issue list (for staff who only have Slack, HubSpot and Famly): every night the FULL list of open issues
(ISSUES codes) is posted, one heading per issue type with what is wrong and how to fix it, then one bullet per
child: the child's name and the HubSpot deal link (no Famly id). An issue that was not there the night before is
marked new (state in registry_refresh_state.json beside the registry). No open issues: one all-clear line.
Issues on deals that are closed (Left Nursery, Closed Lost, Ad Hoc Completed), or on a child whose every deal is
closed, are left out. Names go to Slack and reports/ only, never the log.
"""

import csv
import json
import logging
import sqlite3
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
STATE_FILE_NAME = "registry_refresh_state.json"
MAX_MESSAGE_CHARS = 3500          # one Slack message; a longer list is split at line breaks
NEW_MARK = " 🆕"
RECHECK = "Fixed ones drop off this list after the next nightly check."

# The issues worth a person's attention: (heading, how to fix it). Others (deal_no_child_found, child_without_dob,
# reference_not_operations_or_adhoc) stay in the CSV only.
ISSUES = {
    backfill.RECORDED_CHILD_MISSING: (
        "The deal's Famly child id points at a child that is not in Famly",
        "Find the child in Famly and copy the id from the end of their profile link into the deal's Famly child "
        "id field. If the child was never created in Famly, clear the field."),
    backfill.DEAL_SEVERAL_CHILDREN: (
        "The deal is linked to more than one Famly child (a possible duplicate)",
        "In Famly, check which child is the right one. Archive the duplicate (or remove the deal from its "
        "references), and make sure the deal's Famly child id is the right child."),
    backfill.URN_SEVERAL_CHILDREN: (
        "Two or more Famly children have the same URN",
        "In Famly, open each child and correct the URN on the wrong one, or archive the duplicate child."),
    backfill.STORE_CONFLICT: (
        "The deal or URN now points at a different child than before",
        "Check the deal's Famly child id in HubSpot and the child's URN in Famly. If both are right now, ask an "
        "admin to update the children registry."),
    CHILD_GONE: (
        "The child is no longer in Famly (deleted or merged)",
        "If the child was deleted by mistake, restore them in Famly. If two children were merged on purpose, make "
        "sure the deal's Famly child id is the child that was kept, then ask an admin to clear the old one."),
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


class Lookup:
    """Names and deals for an issue row, from Famly first, then the registry, then the deal. In memory only."""

    def __init__(self, children: dict, deal_names: dict, closed: set, child_deals: dict | None = None):
        self.children, self.deal_names, self.closed = children, deal_names, closed
        self.child_deals = child_deals or {}         # child id -> deal ids tied to it in THIS run's build
        self._registry: dict = {}

    def _row(self, child_id: str):
        if child_id not in self._registry:
            self._registry[child_id] = store.by_child_id(child_id) if child_id else None
        return self._registry[child_id]

    def _child_deals(self, child_id: str) -> list[str]:
        """Every deal known for the child: the registry row, Famly's HUBSPOT references and this run's build."""
        if not child_id:
            return []
        found = [d for d, _ in (self._row(child_id).deals if self._row(child_id) else [])]
        famly = self.children.get(child_id)
        refs = [f for s, f in (famly.references if famly else []) if s == "HUBSPOT" and f]
        for deal in refs + sorted(self.child_deals.get(child_id, ())):
            if deal not in found:
                found.append(deal)
        return found

    def deals(self, child_id: str, deal_id: str) -> list[str]:
        """The deals shown on the issue's line."""
        return [deal_id] if deal_id else self._child_deals(child_id)

    def name(self, child_id: str, deal_id: str) -> str:
        famly = self.children.get(child_id)
        if famly and famly.full_name:
            return famly.full_name
        row = self._row(child_id)
        if row and row.full_name:
            return row.full_name
        for deal in self.deals(child_id, deal_id):
            if self.deal_names.get(deal):
                return self.deal_names[deal]
        return ""

    def is_closed(self, child_id: str, deal_id: str) -> bool:
        """True only when the issue's deal AND every deal known for its child are closed: one open deal keeps it."""
        deals = set(self._child_deals(child_id)) | ({deal_id} if deal_id else set())
        return bool(deals) and all(d in self.closed for d in deals)


def issues_text(issues: set, new: set, lookup: Lookup) -> list[str]:
    """The Slack messages (split when long): the full list of open issues grouped by type."""
    from actions.onboard import notifier
    from integrations import slack

    if not issues:
        return ["✅ *Children registry check: no open issues.*"]
    lines = [f"*Children registry check: {len(issues)} open issue(s)*" + (f" ({len(new)} new{NEW_MARK})"
                                                                         if new else "")]
    by_code: dict = {}
    for item in issues:
        by_code.setdefault(item[0], []).append(item)
    for code in [c for c in ISSUES if c in by_code]:
        heading, fix = ISSUES[code]
        lines += ["", f"*{heading}* ({len(by_code[code])})", f"_How to fix:_ {fix}"]
        bullets = []
        for item in by_code[code]:
            _, child_id, deal_id = item
            name = slack._slack_safe_name(lookup.name(child_id, deal_id)) or "Name not known"
            deals = lookup.deals(child_id, deal_id)
            where = ("deal " + ", ".join(notifier.deal_ref(d) for d in deals)) if deals else "no deal found"
            bullets.append((name.casefold(), f"• {name}: {where}" + (NEW_MARK if item in new else "")))
        lines += [text for _, text in sorted(bullets)]
    lines += ["", RECHECK]
    messages, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    return messages + [current]


def write_report(rows, reports_dir: Path, today: date, tag: str = "", lookup: Lookup | None = None) -> Path:
    """reports/registry_refresh[_dryrun]_YYYY-MM-DD.csv (codes, ids, the child's name and the issue), keeping the
    last KEEP_REPORTS. reports/ is gitignored, so names are allowed there."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"registry_refresh{tag}_{today.isoformat()}.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "famly_child_id", "deal_id", "child_name", "issue"])
        for code, child_id, deal_id in rows:
            name = lookup.name(child_id, deal_id) if lookup and code in ISSUES else ""
            writer.writerow([code, child_id, deal_id, name, ISSUES[code][0] if code in ISSUES else ""])
    for old in sorted(reports_dir.glob(f"registry_refresh{tag}_2*.csv"), reverse=True)[KEEP_REPORTS:]:
        old.unlink()
    return target


def run(hubspot, gql, *, dry_run: bool = False, post=None, today: date | None = None,
        state_file: Path | None = None, reports_dir: Path | str | None = None, site_ids=None,
        children=None, links=None, site_codes=None) -> dict:
    """The nightly refresh. Returns counts (plus, on a dry run, `messages`: the Slack text, for the terminal only).
    Raises RefreshAborted (after one Slack line) when it stopped before writing; a read failure is reported the
    same way and re-raised. `dry_run`: no registry write, no state, no Slack."""
    from actions.onboard import notifier
    from actions.onboard_audit import runner as audit

    today = today or date.today()
    post = post or notifier.SlackNotifier().notice
    state_file = state_file or state_path()
    reports_dir = Path(reports_dir or REPORTS_DIR)
    site_codes = backfill._site_codes() if site_codes is None else site_codes

    def fail(code: str, partial: bool = False):
        if not dry_run:
            what = ("Some registry rows may already have been added (adds only; nothing was changed or removed)"
                    if partial else "Nothing was written")
            _post(post, f"⚠️ Children registry refresh stopped ({code}). {what}; it runs again tomorrow. Ask an "
                        "admin if this repeats.")
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
    except sqlite3.Error as exc:            # the registry write (every read comes before it)
        raise fail(f"write_failed_{type(exc).__name__}", partial=True) from None
    except Exception as exc:  # noqa: BLE001
        raise fail(f"read_failed_{type(exc).__name__}") from None
    no_child = summary.pop("_no_child_deals")
    lookup = Lookup(children, summary.pop("_deal_names"), summary.pop("_closed_deals"), summary.pop("_child_deals"))

    for child_id in sorted(set(before) - set(children)):
        report.add(CHILD_GONE, child_id)
    issues = {row for row in report.rows if row[0] in ISSUES and not lookup.is_closed(row[1], row[2])}
    skipped_closed = sum(1 for row in set(report.rows) if row[0] in ISSUES) - len(issues)
    tag = "_dryrun" if dry_run else ""          # a dry run never overwrites the nightly files
    target = write_report(report.rows, reports_dir, today, tag, lookup)
    backfill.write_deal_list(str(reports_dir / f"registry_deals_without_child{tag}.txt"), no_child)

    result = {"mode": "dry_run" if dry_run else "refresh", "famlyChildren": len(children),
              "registryBefore": len(before), "childrenGone": report.counts[CHILD_GONE],
              "openIssues": len(issues), "issuesOnClosedDeals": skipped_closed,
              "codes": dict(sorted(report.counts.items())), "written": summary.get("written"),
              "liveDealsWithoutChild": len(no_child), "report": str(target)}

    previous = load_state(state_file)
    if previous is None and state_file.exists():
        logger.warning("registry-refresh: state file unreadable; nothing is marked new this run")
    new = issues - previous if previous is not None else set()
    messages = issues_text(issues, new, lookup)
    if dry_run:
        result["messages"] = messages
        return result

    posted = all([_post(post, text) for text in messages])
    if posted:
        save_state(state_file, issues, today)
    logger.info("registry-refresh: children=%d gone=%d open=%d new=%d closed_skipped=%d posted=%s", len(children),
                report.counts[CHILD_GONE], len(issues), len(new), skipped_closed, posted)
    result.update({"new": len(new), "resolved": len((previous or set()) - issues), "posted": posted,
                   "postFailed": not posted})
    return result
