"""Famly deposit and invoice calls (captured from the Famly UI by Drew, 2026-11-27).

  list deposits   GET  /v2/deposits/{institutionId}/{childId}
                  -> {"deposits": [{depositId, childId, billPayerId, note, amount, date, createdAt,
                                    return, status, behaviors}], "behaviors": [...]}
  add deposits    POST /v2/deposits/{institutionId}/{childId}/{billPayerId}
                  body {"deposits": [{"amount": 12, "date": "YYYY-MM-DD", "note": "", "status": ""}]}
                  status "" = unpaid (read back as PENDING), "PAID_EXTERN" = paid outside Famly.
                  The UI ADDS to the list; whether a server POST does the same is checked by deposit-probe.
  list invoices   GET  /v2/invoicing/invoices?target=Famly.Daycare:Institution|{institutionId}&billPayerIds=...
  create invoice  GraphQL CreateInvoice, mode "Preview" (nothing saved) or "Persist" (the real invoice).
                  An invoice is per BILL PAYER: it carries every child of that payer.

Writes go only through `DepositWriter`, which must be built with `writes_allowed=True` (the worker passes it
only in DEPOSIT_MODE=live; the probe only for a verified test child). No names are requested or returned.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
INVOICE_QUERY = HERE / "create_invoice.graphql"
INVOICE_OP = "CreateInvoice"

STATUS_UNPAID = ""              # sent; Famly reads it back as PENDING
STATUS_PAID = "PAID_EXTERN"
READ_PENDING = "PENDING"
MODE_PREVIEW = "Preview"
MODE_PERSIST = "Persist"


class WritesNotAllowed(RuntimeError):
    pass


def amount_json(amount: Decimal) -> int | float:
    """Pounds as the JSON number Famly expects: 1500 or 375.5. Never more than 2 dp."""
    q = Decimal(amount).quantize(Decimal("0.01"))
    if q != Decimal(amount):
        raise ValueError("amount has more than 2 decimal places")
    return int(q) if q == q.to_integral_value() else float(q)


def amount_read(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def list_deposits(rest, institution_id: str, child_id: str) -> list[dict]:
    body = rest.get(f"/v2/deposits/{institution_id}/{child_id}")
    deposits = body.get("deposits") if isinstance(body, dict) else None
    if not isinstance(deposits, list):
        raise ValueError("deposits list missing from the response")
    return deposits


def list_invoices(rest, institution_id: str, bill_payer_id: str) -> Any:
    return rest.get("/v2/invoicing/invoices",
                    params={"target": f"Famly.Daycare:Institution|{institution_id}",
                            "billPayerIds": bill_payer_id})


def invoice_variables(bill_payer_id: str, invoice_date: str, due_date: str, range_from: str,
                      range_to: str, mode: str, notify: bool) -> dict:
    if mode not in (MODE_PREVIEW, MODE_PERSIST):
        raise ValueError(f"unknown invoice mode {mode!r}")
    return {"requests": [{"billPayerId": bill_payer_id}], "invoiceDate": invoice_date,
            "dueDate": due_date, "mode": mode, "range": {"from": range_from, "to": range_to},
            "notify": bool(notify)}


def invoice_results(body: dict) -> list[dict]:
    """[{ok, invoiceId, total, errors}] from a CreateInvoice response."""
    rows = (((body.get("data") or {}).get("finance") or {}).get("invoices") or {}).get("create") or {}
    out = []
    for item in rows.get("invoices") or []:
        if item.get("__typename") == "InvoiceSuccess":
            inv = item.get("invoice") or {}
            out.append({"ok": True, "invoiceId": inv.get("invoiceId"), "total": inv.get("total"),
                        "invoiceDate": inv.get("invoiceDate"), "errors": []})
        else:
            out.append({"ok": False, "invoiceId": None, "total": None, "invoiceDate": None,
                        "errors": [{"code": e.get("code"), "message": e.get("message")}
                                   for e in item.get("errors") or []]})
    return out


def preview_invoice(gql, bill_payer_id: str, invoice_date: str, due_date: str, range_from: str,
                    range_to: str) -> list[dict]:
    """Preview only: Famly saves nothing and notifies no one."""
    body = gql.execute(query_path=INVOICE_QUERY, operation_name=INVOICE_OP,
                       variables=invoice_variables(bill_payer_id, invoice_date, due_date, range_from,
                                                   range_to, MODE_PREVIEW, False))
    return invoice_results(body)


class DepositWriter:
    """The ONLY path that adds a deposit or persists an invoice."""

    def __init__(self, rest, gql, writes_allowed: bool):
        self.rest, self.gql, self.writes_allowed = rest, gql, writes_allowed

    def _check(self):
        if self.writes_allowed is not True:
            raise WritesNotAllowed("deposit writes are not allowed in this mode")

    def add_deposits(self, institution_id: str, child_id: str, bill_payer_id: str,
                     entries: list[dict]) -> Any:
        """entries: [{amount: Decimal, date: 'YYYY-MM-DD', status: '' | 'PAID_EXTERN', note: str}]"""
        self._check()
        if not (institution_id and child_id and bill_payer_id):
            raise ValueError("an id is missing")
        if not entries:
            raise ValueError("no deposits to add")
        body = {"deposits": [{"amount": amount_json(e["amount"]), "date": e["date"],
                              "note": e.get("note", ""), "status": e["status"]} for e in entries]}
        for row in body["deposits"]:
            if row["status"] not in (STATUS_UNPAID, STATUS_PAID) or row["amount"] <= 0:
                raise ValueError("bad deposit row")
        return self.rest.post(f"/v2/deposits/{institution_id}/{child_id}/{bill_payer_id}", json_body=body)

    def persist_invoice(self, bill_payer_id: str, invoice_date: str, due_date: str, range_from: str,
                        range_to: str, notify: bool) -> list[dict]:
        self._check()
        body = self.gql.execute(query_path=INVOICE_QUERY, operation_name=INVOICE_OP,
                                variables=invoice_variables(bill_payer_id, invoice_date, due_date,
                                                            range_from, range_to, MODE_PERSIST, notify))
        return invoice_results(body)
