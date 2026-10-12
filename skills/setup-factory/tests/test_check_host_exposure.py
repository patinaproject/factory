from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-host-exposure"
loader = importlib.machinery.SourceFileLoader("check_host_exposure", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
check = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = check
loader.exec_module(check)

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1053:cb6:7381:549d%en1 prefixlen 64 secured scopeid 0xb
\tinet 192.0.2.54 netmask 0xffffff00 broadcast 192.0.2.255
\tinet6 2001:db8::d2f4 prefixlen 64 autoconf secured
\tinet6 2001:db8::73e8 prefixlen 64 autoconf temporary
"""


class ExposedAddressesTest(unittest.TestCase):
    def test_lists_only_addresses_other_hosts_can_reach(self) -> None:
        self.assertEqual(check.exposed_addresses(IFCONFIG), ["192.0.2.54", "2001:db8::d2f4", "2001:db8::73e8"])


class SshAuthMethodsTest(unittest.TestCase):
    def test_reads_the_offered_methods(self) -> None:
        output = "Warning: Permanently added '192.0.2.54'.\r\nnobody@192.0.2.54: Permission denied (publickey,password,keyboard-interactive).\r\n"
        self.assertEqual(check.ssh_auth_methods(output), ["publickey", "password", "keyboard-interactive"])


class PrincipalsForTest(unittest.TestCase):
    def test_uses_each_email_local_part(self) -> None:
        self.assertEqual(check.principals_for(["ana@example.com", " bo@example.com"]), {"ana", "bo"})

    def test_refuses_a_domain(self) -> None:
        with self.assertRaisesRegex(ValueError, "never a domain"):
            check.principals_for(["example.com"])


class EvaluateTest(unittest.TestCase):
    def failures(self, facts) -> list[str]:
        return [message for ok, message in check.evaluate(facts) if not ok]

    def test_fails_on_ssh_password_login(self) -> None:
        facts = check.Facts(ssh_methods={"192.0.2.54": ["publickey", "password", "keyboard-interactive"]})
        self.assertEqual(
            self.failures(facts),
            [f"SSH on 192.0.2.54 offers password login (password, keyboard-interactive): {check.SSH_FIX}"],
        )

    def test_fails_on_each_reachable_sharing_service(self) -> None:
        facts = check.Facts(open_ports={"2001:db8::d2f4": [445, 7000]})
        self.assertEqual(
            self.failures(facts),
            [
                f"File Sharing (SMB) is reachable on 2001:db8::d2f4:445: {check.SHARING_OFF}",
                "AirPlay Receiver is reachable on 2001:db8::d2f4:7000: "
                "turn it off in System Settings > General > AirDrop & Handoff > AirPlay Receiver",
            ],
        )

    def test_fails_when_the_firewall_is_off(self) -> None:
        self.assertEqual(
            self.failures(check.Facts(firewall_enabled=False)),
            ["the macOS application firewall is off: turn it on in System Settings > Network > Firewall"],
        )

    def test_fails_when_principals_drift_from_the_allowlist(self) -> None:
        facts = check.Facts(principals={"ana", "old"}, expected_principals={"ana", "bo", "cy"})
        self.assertEqual(
            self.failures(facts),
            [
                "SSH principals differ from the Access SSH allowlist (missing: bo, cy; extra: old): "
                "write one email local part per line"
            ],
        )

    def test_fails_when_ssh_reports_no_login_methods(self) -> None:
        facts = check.Facts(ssh_methods={"192.0.2.54": []})
        self.assertEqual(
            self.failures(facts),
            ["SSH on 192.0.2.54 did not report its login methods: check that sshd answers on this port"],
        )

    def test_fails_when_no_address_was_probed(self) -> None:
        self.assertEqual(
            self.failures(check.Facts(addresses=[])),
            ["found no address another host can reach, so nothing was probed: pass --address"],
        )

    def test_fails_when_the_principals_file_cannot_be_read(self) -> None:
        facts = check.Facts(principals=None, expected_principals={"ana"})
        self.assertEqual(
            self.failures(facts),
            ["the SSH principals file cannot be read: run the check as the account people SSH into"],
        )

    def test_passes_a_hardened_machine(self) -> None:
        facts = check.Facts(
            ssh_methods={"192.0.2.54": ["publickey"]},
            open_ports={"192.0.2.54": []},
            firewall_enabled=True,
            principals={"ana"},
            expected_principals={"ana"},
        )
        self.assertEqual(
            check.evaluate(facts),
            [
                (True, "SSH on 192.0.2.54 accepts only keys and certificates"),
                (True, "no sharing service is reachable"),
                (True, "the macOS application firewall is on"),
                (True, "SSH principals match the Access SSH allowlist"),
            ],
        )



class MainTest(unittest.TestCase):
    def test_a_domain_in_the_allowlist_is_a_fail_line(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--address", "127.0.0.1", "--ssh-users", "example.com"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout,
            "FAIL 'example.com' is not an email address; the SSH allowlist names people, never a domain\n",
        )
        self.assertEqual(result.stderr, "")

if __name__ == "__main__":
    unittest.main()
