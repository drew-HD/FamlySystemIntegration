"""The ChildInput for a NEW ad hoc child. Same payload as actions/onboard/inputs.build_child_input but with NO
`lastDay` (an ad hoc child has no end date), no `isTestChild`, and the URN allocated for the deal. Personal data is
read from the in-memory deal, put in the payload and dropped; nothing here logs or stores a value."""

from datetime import date

from actions.onboard.inputs import InputIncomplete, full_name
from actions.onboard_dry_run import derive


def build_adhoc_child_input(deal: dict, deal_id: str, site_id: str, group_id: str, first_day: date, urn: str) -> dict:
    name = full_name(deal.get("child_first_name"), deal.get("child_last_name"))
    if name is None:
        raise InputIncomplete("child_name_incomplete")
    try:
        birthday = derive.parse_hubspot_date(deal.get("child_date_of_birth"))
    except ValueError:
        raise InputIncomplete("date_invalid") from None
    if birthday is None:
        raise InputIncomplete("dob_missing")
    if not group_id or not site_id:
        raise InputIncomplete("group_missing")
    if not urn:
        raise InputIncomplete("urn_missing")
    return {
        "name": name,
        "birthday": birthday.strftime("%Y-%m-%d"),
        "currentGroupId": group_id,
        "records": [],
        "sensitiveRecords": [],
        "sitesRelation": [{"siteId": site_id, "firstDay": first_day.strftime("%Y-%m-%d")}],   # NO lastDay
        "externalId": urn,
        "externalSystemsReferences": [{"foreignId": str(deal_id), "system": "HUBSPOT"},
                                      {"foreignId": urn, "system": "URN"}],
    }
