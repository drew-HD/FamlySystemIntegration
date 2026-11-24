"""Posts the ad hoc messages through integrations.slack (same channel as onboarding). Never raises. The worker takes
any object with these methods, so tests pass a fake and scan what it was handed."""

from integrations import slack

from . import approval


class SlackNotifier:
    def gate(self, text: str, job_id: str, plan_hash: str, nonce: str) -> str | None:
        return slack.post_adhoc_gate(text, approval.button_value(job_id, plan_hash, nonce))

    def match(self, text: str, job_id: str, plan_hash: str, nonce: str) -> str | None:
        return slack.post_adhoc_match(text, approval.button_value(job_id, plan_hash, nonce))

    def hold(self, text: str, job_id: str, nonce: str) -> str | None:
        return slack.post_adhoc_hold(text, approval.button_value(job_id, "", nonce))

    def notice(self, text: str) -> bool:
        return slack.post_notice(text)
