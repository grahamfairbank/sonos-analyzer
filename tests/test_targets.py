import unittest

from sonos_analyzer.targets import clean_text, is_allowed_target


class TargetTests(unittest.TestCase):
    def test_allowed(self):
        for host in ["10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.20", "100.64.0.1", "100.127.255.1", " 192.168.1.20 "]:
            with self.subTest(host=host):
                self.assertTrue(is_allowed_target(host))

    def test_refused(self):
        for host in [
            None, "", "speaker.local", "8.8.8.8", "172.32.0.1", "127.0.0.1", "0.0.0.0", "169.254.1.1",
            "224.0.0.251", "239.255.255.250", "255.255.255.255", "203.0.113.5", "198.51.100.1", "192.0.2.1",
            "100.128.0.1", "::1", "fd00::1", "192.168.1", "192.168.1.300",
        ]:
            with self.subTest(host=host):
                self.assertFalse(is_allowed_target(host))

    def test_clean_text(self):
        self.assertEqual(clean_text("Kitchen\x1b[2J\x9b31m\x00 & Dining"), "Kitchen[2J31m & Dining")
        self.assertEqual(clean_text("Café Ünïcode ✓"), "Café Ünïcode ✓")


if __name__ == "__main__":
    unittest.main()
