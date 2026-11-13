import socket
import unittest
from unittest import mock

from sonos_analyzer import app


class BindTests(unittest.TestCase):
    def test_skips_busy_port(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        with mock.patch.object(app, "FIRST_PORT", port), mock.patch.object(app, "PORTS_TO_TRY", 5):
            server = app._bind(None)
        self.addCleanup(server.server_close)
        self.assertNotEqual(server.server_address[1], port)
        self.assertEqual(server.server_address[0], "127.0.0.1")

    def test_requested_port_busy(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        self.addCleanup(busy.close)
        with self.assertRaises(SystemExit):
            app._bind(busy.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
