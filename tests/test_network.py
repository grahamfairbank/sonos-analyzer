"""Subnet and double-NAT checks, with the router replaced by canned UPnP replies."""

import unittest
from unittest import mock

from sonos_analyzer import network
from sonos_analyzer.analyze import analyze
from sonos_analyzer.parse import Gateway, RouterCheck
from tests.helpers import finding, snapshot

LOCATION = "http://192.168.50.1:5000/rootDesc.xml"


def description(control_url="/ctl/IPConn", service="urn:schemas-upnp-org:service:WANIPConnection:1"):
    return (
        '<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        "<friendlyName>Home router</friendlyName><modelName>ExampleRouter AX</modelName>"
        "<deviceList><device><deviceList><device><serviceList><service>"
        f"<serviceType>{service}</serviceType><controlURL>{control_url}</controlURL>"
        "</service></serviceList></device></deviceList></device></deviceList>"
        "</device></root>"
    )


def wan_reply(address):
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">'
        f"<NewExternalIPAddress>{address}</NewExternalIPAddress>"
        "</u:GetExternalIPAddressResponse></s:Body></s:Envelope>"
    )


class ClassifyTests(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(network.classify_wan("10.0.0.7"), "private")
        self.assertEqual(network.classify_wan("192.168.0.2"), "private")
        self.assertEqual(network.classify_wan("100.72.1.9"), "cgnat")
        self.assertEqual(network.classify_wan("0.0.0.0"), "none")
        self.assertEqual(network.classify_wan("203.0.113.9"), "private")  # documentation range, not routable
        self.assertEqual(network.classify_wan("8.8.4.4"), "public")
        self.assertEqual(network.classify_wan(""), "unknown")


class DescribeGatewayTests(unittest.TestCase):
    def run_with(self, desc, reply):
        with mock.patch.object(network, "_get", return_value=desc), mock.patch.object(
            network, "soap_request", return_value=reply
        ) as soap:
            return network.describe_gateway(LOCATION), soap

    def test_double_nat(self):
        g, soap = self.run_with(description(), wan_reply("192.168.1.20"))
        self.assertEqual((g.ip, g.model, g.wan, g.wan_ip), ("192.168.50.1", "ExampleRouter AX", "private", "192.168.1.20"))
        self.assertEqual(soap.call_args[0][0], "http://192.168.50.1:5000/ctl/IPConn")

    def test_public_address_is_not_kept(self):
        g, _ = self.run_with(description(), wan_reply("8.8.4.4"))
        self.assertEqual((g.wan, g.wan_ip), ("public", ""))

    def test_control_url_on_another_host_is_refused(self):
        g, soap = self.run_with(description("http://192.168.50.9/ctl"), wan_reply("10.0.0.1"))
        soap.assert_not_called()
        self.assertEqual(g.wan, "unknown")
        self.assertIn("elsewhere", g.error)

    def test_public_location_is_refused(self):
        with mock.patch.object(network, "_get") as get:
            g = network.describe_gateway("http://8.8.8.8:5000/rootDesc.xml")
        get.assert_not_called()
        self.assertEqual(g.wan, "unknown")

    def test_bad_reply_is_unknown(self):
        g, _ = self.run_with("<not xml", "")
        self.assertEqual(g.wan, "unknown")
        self.assertTrue(g.error)

    def test_check_router_dedupes_and_caps(self):
        locations = [LOCATION, LOCATION] + [f"http://192.168.50.{n}:80/d.xml" for n in range(2, 9)]
        with mock.patch.object(network, "search_gateways", return_value=locations), mock.patch.object(
            network, "describe_gateway", side_effect=lambda loc: Gateway(ip=loc.split("/")[2].split(":")[0])
        ):
            check = network.check_router()
        self.assertEqual(len(check.gateways), network.MAX_GATEWAYS)
        self.assertEqual(len({g.ip for g in check.gateways}), network.MAX_GATEWAYS)


class FindingTests(unittest.TestCase):
    def test_netmask_parsed(self):
        snap = snapshot()
        self.assertTrue(all(sp.netmask == "255.255.255.0" for sp in snap.speakers.values()))

    def test_one_subnet_no_findings(self):
        report = analyze(snapshot())
        self.assertFalse([f for f in report.findings if f.category == "network"])

    def test_split_subnets(self):
        snap = snapshot()
        moved = next(sp for sp in snap.speakers.values() if not sp.is_invisible)
        moved.ip_address = "192.168.60.5"
        f = finding(analyze(snap), "on a different network from the rest")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.affected_ips, ["192.168.60.5"])
        self.assertIn("same subnet", f.detail)

    def test_wide_mask_on_surround_is_not_a_split(self):
        snap = snapshot()
        # Home theater surrounds on the soundbar's direct link report 255.0.0.0 (seen on Era 300s).
        for sp in list(snap.speakers.values())[:2]:
            sp.is_invisible = True
            sp.netmask = "255.0.0.0"
        self.assertFalse([f for f in analyze(snap).findings if f.category == "network"])

    def test_computer_on_other_network(self):
        snap = snapshot()
        snap.analyzer_addresses = {ip: "192.168.13.250" for ip in snap.speakers}
        f = finding(analyze(snap), "different network from the speakers")
        self.assertEqual(f.severity, "low")
        self.assertIn("192.168.50.0/24", f.recommendation)

    def test_computer_on_same_network(self):
        snap = snapshot()
        snap.analyzer_addresses = {ip: "192.168.50.200" for ip in snap.speakers}
        self.assertIsNone(finding(analyze(snap), "different network from the speakers"))

    def test_double_nat_is_informational(self):
        snap = snapshot()
        snap.router = RouterCheck(searched=True, gateways=[Gateway("192.168.50.1", "ExampleRouter", "private", "192.168.1.20")])
        report = analyze(snap)
        f = finding(report, "Double NAT")
        self.assertEqual(f.severity, "info")
        self.assertIn("192.168.1.20", f.detail)
        self.assertNotIn("No double NAT", [x.title for x in report.findings])

    def test_public_wan_is_ok(self):
        snap = snapshot()
        snap.router = RouterCheck(searched=True, gateways=[Gateway("192.168.50.1", "ExampleRouter", "public")])
        f = finding(analyze(snap), "No double NAT")
        self.assertEqual(f.severity, "ok")
        self.assertNotIn("8.8", f.detail)

    def test_cgnat_and_two_routers(self):
        snap = snapshot()
        snap.router = RouterCheck(
            searched=True,
            gateways=[Gateway("192.168.50.1", "A", "cgnat", "100.72.1.9"), Gateway("192.168.50.2", "B", "public")],
        )
        report = analyze(snap)
        self.assertEqual(finding(report, "carrier-grade NAT").severity, "info")
        self.assertEqual(finding(report, "2 routers answered").severity, "info")
        self.assertIsNone(finding(report, "No double NAT"))

    def test_no_router_answer_says_nothing(self):
        snap = snapshot()
        snap.router = RouterCheck(searched=True)
        self.assertFalse([f for f in analyze(snap).findings if f.category == "network"])


if __name__ == "__main__":
    unittest.main()
