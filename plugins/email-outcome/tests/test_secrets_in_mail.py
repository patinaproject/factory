from __future__ import annotations

import unittest

import secrets_in_mail

EMAIL = (
    "Hi,\n\nYour verification code is 482913. It expires in 10 minutes.\n"
    "Reset your password at https://accounts.vendor.example/reset?token=abc123XYZ.\n"
    "Your new API key is sk-live_0123456789abcdefABCDEF.\n"
)
INVOICE = "Invoice 482913 for October is attached. Amount due: $120.00.\n"


def unchanged(text):
    return text


class RefusalTest(unittest.TestCase):
    def test_a_code_a_reset_link_and_a_key_from_the_email_are_each_refused(self):
        for write, reason in (
            ("The vendor sent the sign-in number 482913.", "code word"),
            ("Use https://accounts.vendor.example/reset?token=abc123XYZ to reset.", "reset, token, verify, or magic"),
            ("The key is sk-live_0123456789abcdefABCDEF.", "secret redaction"),
        ):
            with self.subTest(reason=reason):
                refused = secrets_in_mail.refusal(write, EMAIL, unchanged)
                self.assertIn(reason, refused)
                self.assertNotIn("482913", refused)

    def test_the_same_number_as_an_invoice_with_no_code_word_is_allowed(self):
        self.assertIsNone(secrets_in_mail.refusal("Pay invoice 482913 by November 20.", INVOICE, unchanged))

    def test_a_write_without_the_values_is_allowed(self):
        self.assertIsNone(secrets_in_mail.refusal("The vendor sent a sign-in code and a password reset link.", EMAIL, unchanged))

    def test_a_link_from_the_email_without_a_sensitive_part_is_allowed(self):
        email = "Read the notice at https://status.vendor.example/incidents/42 today."
        self.assertIsNone(secrets_in_mail.refusal("See https://status.vendor.example/incidents/42", email, unchanged))

    def test_whatever_hermes_redaction_would_change_is_refused(self):
        self.assertIn(
            "secret redaction",
            secrets_in_mail.refusal("password=hunter22", "", lambda text: text.replace("hunter22", "***")),
        )


if __name__ == "__main__":
    unittest.main()
