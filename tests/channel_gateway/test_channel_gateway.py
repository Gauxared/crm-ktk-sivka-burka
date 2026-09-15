import unittest
from types import MappingProxyType

from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext, ChannelGateway, DraftSaved, GatewayError, InquiryReceipt,
    Platform, ResetResult,
)


class Port:
    def __init__(self): self.calls = []
    def read_catalog(self, c): self.calls.append(("catalog", c)); return {"catalog_version": 1}
    def read_draft(self, c): self.calls.append(("draft", c)); return None
    def save_draft(self, c, expected_version, answers, step, event_id):
        self.calls.append(("save", c, expected_version, answers, step, event_id)); return DraftSaved(2, step)
    def submit_inquiry(self, c, fields, draft_version, submit_event_id):
        self.calls.append(("submit", c, fields, draft_version, submit_event_id)); return InquiryReceipt("r", "i")
    def reset_draft(self, c, expected_version, event_id):
        self.calls.append(("reset", c, expected_version, event_id)); return ResetResult(True, expected_version + 1)


def context(platform=Platform.TELEGRAM, event="evt-1"):
    return ChannelContext(platform, "integration", "sender", "conversation", event)


class ChannelGatewayTests(unittest.TestCase):
    def setUp(self): self.port = Port(); self.gateway = ChannelGateway(self.port)

    def test_telegram_and_vk_are_equivalent_but_namespaced(self):
        self.gateway.read_catalog(context(Platform.TELEGRAM))
        self.gateway.read_catalog(context(Platform.VK))
        self.assertNotEqual(self.port.calls[0][1].namespace(), self.port.calls[1][1].namespace())

    def test_context_is_trusted_immutable_and_user_fields_cannot_override_it(self):
        c = context(); self.gateway.save_draft(c, expected_version=0, answers={"name": "x"}, step="contact", event_id="evt-1")
        self.assertEqual(self.port.calls[-1][1], c)
        with self.assertRaises(GatewayError):
            self.gateway.submit_inquiry(c, fields={"platform": "VK", "service_option_id": "s", "participants_count": 1}, draft_version=1, submit_event_id="evt-1")
        self.assertEqual(len(self.port.calls), 1)
        with self.assertRaises(Exception): c.platform = Platform.VK

    def test_strict_form_and_versions_reject_without_mutation(self):
        cases = [
            lambda: self.gateway.submit_inquiry(context(), fields={"service_option_id": "s", "participants_count": 1, "extra": 1}, draft_version=1, submit_event_id="evt-1"),
            lambda: self.gateway.submit_inquiry(context(), fields={"service_option_id": "s", "participants_count": 0}, draft_version=1, submit_event_id="evt-1"),
            lambda: self.gateway.save_draft(context(), expected_version=-1, answers={}, step="x", event_id="evt-1"),
            lambda: self.gateway.reset_draft(context(), expected_version=1, event_id="other"),
        ]
        for operation in cases:
            with self.assertRaises(GatewayError): operation()
        self.assertEqual(self.port.calls, [])

    def test_submit_requires_normalized_business_fields_and_matching_event(self):
        with self.assertRaises(GatewayError):
            self.gateway.submit_inquiry(context(), fields={"service_option_id": "s", "participants_count": 1}, draft_version=1, submit_event_id="other")
        self.assertEqual(self.port.calls, [])
        result = self.gateway.submit_inquiry(context(), fields={"service_option_id": "s", "participants_count": 2}, draft_version=1, submit_event_id="evt-1")
        self.assertEqual(result.receipt_id, "r")


if __name__ == "__main__": unittest.main()
