"""actions/adhoc/writes.py: the three guarded Famly writes. Every guard and every outcome class, with fakes only:
the booking is sent once and never retried, an unclear outcome is never guessed, the tag and the end date are settled by
reads, and there is no delete anywhere."""

import unittest

from actions.adhoc import writes
from core.client import GraphQLError, GraphQLHTTPError

CHILD = "child-1"
PURCHASE = {"childId": CHILD, "discountAmount": "7.50", "productBookings": [],
            "sessionBookings": [
                {"bookedPrice": "75.00", "pricingGroupId": "g1", "purchaseDate": "2026-12-28", "sessionAvailabilityId": "a1"},
                {"bookedPrice": "75.00", "pricingGroupId": "g1", "purchaseDate": "2026-12-29", "sessionAvailabilityId": "a2"}]}


def answer(**over):
    purchase = {"id": "p1", "discountAmount": "7.50", "total": "142.50",
                "sessionBookings": [{"sessionPurchaseId": "s1", "purchaseDate": "2026-12-28", "bookedPrice": "75.00", "pricingGroupId": "g1"},
                                    {"sessionPurchaseId": "s2", "purchaseDate": "2026-12-29", "bookedPrice": "75.00", "pricingGroupId": "g1"}]}
    purchase.update(over)
    return {"data": {"finance": {"purchases": {"update": [purchase]}}}}


class Gql:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def execute(self, query_path, variables, operation_name):
        self.calls.append((operation_name, variables))
        if self.error:
            raise self.error
        return self.response


class Rest:
    def __init__(self, tags=None, child=None, found=None, post_error=None, after=None):
        self.tag_rows = found if found is not None else [{"tagId": "t1", "title": "Ad Hoc Only", "institutionId": "inst", "deletedAt": None}]
        self.have, self.child, self.after = list(tags or []), child or {"endDate": None, "localEndDate": ""}, after
        self.posts, self.post_error, self.reads = [], post_error, 0

    def get(self, path, params=None, **kw):
        if path == "/v2/tags":
            return self.tag_rows
        self.reads += 1
        child = self.after if (self.after is not None and self.reads > 1) else self.child
        return {"child": child, "tags": [{"tagId": t} for t in self.have]}

    def post(self, path, params=None, json_body=None):
        self.posts.append((path, json_body))
        if self.post_error:
            raise self.post_error
        self.have.append(path.split("/")[3])
        return {}


class DocumentTests(unittest.TestCase):
    def test_documents_are_the_booking_and_end_date_only_and_nothing_deletes(self):
        writes.assert_documents()
        for path in (writes.UPSERT_QUERY, writes.END_DATE_QUERY):
            self.assertNotIn("delete", writes._doc_text(path))      # the document body, comments stripped
        self.assertFalse(hasattr(writes.FamlyWriter, "delete"))


class BookTests(unittest.TestCase):
    def writer(self, **kw):
        self.gql = Gql(**kw)
        return writes.FamlyWriter(self.gql, Rest())

    def test_the_approved_purchase_is_sent_once_and_checked_against_the_answer(self):
        result = self.writer(response=answer()).book(CHILD, PURCHASE)
        self.assertEqual((result.purchase_ids, result.verified, result.mismatches), (["p1"], True, []))
        self.assertEqual([c[0] for c in self.gql.calls], ["UpsertPurchase"])
        self.assertEqual(self.gql.calls[0][1], {"purchases": [PURCHASE]})

    def test_a_purchase_for_another_child_or_with_products_or_empty_is_refused_before_any_request(self):
        for bad in (dict(PURCHASE, childId="other"), dict(PURCHASE, productBookings=[{"x": 1}]),
                    dict(PURCHASE, sessionBookings=[])):
            w = self.writer(response=answer())
            with self.assertRaises(writes.BookingRejected):
                w.book(CHILD, bad)
            self.assertEqual(self.gql.calls, [])

    def test_what_famly_stored_must_match_what_was_approved(self):
        for over, expect in ((dict(discountAmount="0.00"), ["discount"]),
                             (dict(sessionBookings=answer()["data"]["finance"]["purchases"]["update"][0]["sessionBookings"][:1]), ["session_rows"]),
                             (dict(sessionBookings=[{"purchaseDate": "2026-12-28", "bookedPrice": "80.00", "pricingGroupId": "g1"},
                                                    {"purchaseDate": "2026-12-29", "bookedPrice": "75.00", "pricingGroupId": "g1"}]), ["session_rows"])):
            result = writes.check_booking(PURCHASE, answer(**over))
            self.assertEqual((result.verified, result.mismatches), (False, expect), over)
        two = {"data": {"a": answer()["data"], "b": answer(id="p2")["data"]}}
        self.assertEqual(writes.check_booking(PURCHASE, two).mismatches, ["purchase_count"])
        self.assertEqual(writes.check_booking(PURCHASE, {"data": None}).verified, False)

    def test_outcome_classes_rejected_versus_unknown_and_never_retried(self):
        cases = [(GraphQLError("bad", [{"message": "x"}], {"data": None}), writes.BookingRejected),
                 (GraphQLError("bad", [{"message": "x"}], {"data": {"finance": {}}}), writes.BookingUnknown),   # errors WITH data
                 (GraphQLHTTPError("bad", 422, "body"), writes.BookingRejected),
                 (GraphQLHTTPError("bad", 503, "body"), writes.BookingUnknown),
                 (TimeoutError("slow"), writes.BookingUnknown)]
        for error, expected in cases:
            w = self.writer(error=error)
            with self.assertRaises(expected):
                w.book(CHILD, PURCHASE)
            self.assertEqual(len(self.gql.calls), 1, error)            # exactly one request, never a second


class AdministrativeTests(unittest.TestCase):
    def test_tag_is_assigned_once_and_confirmed_by_reading_the_child(self):
        rest = Rest()
        self.assertEqual(writes.FamlyWriter(Gql(), rest).tag(CHILD, "inst"), writes.TAG_ADDED)
        self.assertEqual(rest.posts, [("/v2/tags/t1/assign", {"childId": CHILD})])
        rest = Rest(tags=["t1"])
        self.assertEqual(writes.FamlyWriter(Gql(), rest).tag(CHILD, "inst"), writes.TAG_PRESENT)
        self.assertEqual(rest.posts, [])

    def test_a_tag_that_is_missing_or_ambiguous_is_never_guessed(self):
        for rows in ([], [{"tagId": "a", "title": "Ad Hoc Only", "institutionId": "inst"}, {"tagId": "b", "title": "Ad Hoc Only", "institutionId": "inst"}],
                     [{"tagId": "a", "title": "Ad Hoc Only", "institutionId": "inst", "deletedAt": "2026-01-01"}],
                     [{"tagId": "a", "title": "Ad Hoc Only", "institutionId": "other-site"}]):
            rest = Rest(found=rows)
            self.assertEqual(writes.FamlyWriter(Gql(), rest).tag(CHILD, "inst"), writes.TAG_NOT_FOUND)
            self.assertEqual(rest.posts, [])

    def test_a_tag_request_that_errors_is_settled_by_a_read_not_sent_again(self):
        class Lands(Rest):
            def post(self, path, params=None, json_body=None):
                self.posts.append(path)
                self.have.append("t1")                   # it landed, but the answer was an error
                raise TimeoutError("slow")
        rest = Lands()
        self.assertEqual(writes.FamlyWriter(Gql(), rest).tag(CHILD, "inst"), writes.TAG_ADDED)
        self.assertEqual(len(rest.posts), 1)
        rest = Rest(post_error=TimeoutError("slow"))     # and one that really did not land
        rest.have = []
        rest.post = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("slow"))
        self.assertEqual(writes.FamlyWriter(Gql(), rest).tag(CHILD, "inst"), writes.TAG_FAILED)

    def test_end_date_removal_sends_start_date_only_and_is_confirmed_by_reading(self):
        child = {"endDate": "2026-07-31", "localEndDate": "2026-07-31", "localStartDate": "2026-01-05"}
        gql, rest = Gql(response={}), Rest(child=child, after={"endDate": None, "localEndDate": "", "localStartDate": "2026-01-05"})
        self.assertEqual(writes.FamlyWriter(gql, rest).remove_end_date(CHILD), writes.END_REMOVED)
        self.assertEqual(gql.calls, [("EditChildActiveDates", {"childId": CHILD, "childActiveDates": {"startDate": "2026-01-05"},
                                                              "isParent": False})])

    def test_no_end_date_means_no_request_and_a_missing_start_date_is_never_guessed(self):
        gql = Gql()
        self.assertEqual(writes.FamlyWriter(gql, Rest()).remove_end_date(CHILD), writes.END_NONE)
        self.assertEqual(gql.calls, [])
        gql = Gql()
        no_start = Rest(child={"endDate": "2026-07-31", "localEndDate": "2026-07-31", "localStartDate": ""})
        self.assertEqual(writes.FamlyWriter(gql, no_start).remove_end_date(CHILD), writes.END_FAILED)
        self.assertEqual(gql.calls, [])

    def test_an_end_date_that_is_still_there_afterwards_is_a_failure(self):
        child = {"endDate": "2026-07-31", "localEndDate": "2026-07-31", "localStartDate": "2026-01-05"}
        gql, rest = Gql(error=TimeoutError("slow")), Rest(child=child)
        self.assertEqual(writes.FamlyWriter(gql, rest).remove_end_date(CHILD), writes.END_FAILED)
        self.assertEqual(len(gql.calls), 1)


if __name__ == "__main__":
    unittest.main()
