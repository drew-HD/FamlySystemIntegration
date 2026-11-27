"""Posts the deposit messages through integrations.slack (same channel as onboarding). Never raises. The worker
takes any object with these methods, so tests pass a fake."""

from integrations import slack

from . import approval


class SlackNotifier:
    def gate(self, text: str, job_id: str, nonce: str) -> str | None:
        return slack.post_deposit_gate(text, approval.button_value(job_id, nonce))

    def notice(self, text: str) -> bool:
        return slack.post_notice(text)
