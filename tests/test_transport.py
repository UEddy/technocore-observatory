import unittest
import urllib.request

from observatory import transport

FIXTURE = "fixtures/rooms-sample.txt"


class NoNetworkDuringFixtureRunsTests(unittest.TestCase):
    """The fixture transport must be incapable of reaching the service."""

    def setUp(self):
        self.original = urllib.request.urlopen

        def refuse(*_args, **_kwargs):
            raise AssertionError("a fixture run attempted a live request")

        urllib.request.urlopen = refuse
        self.addCleanup(setattr, urllib.request, "urlopen", self.original)

    def test_fixture_get_reads_from_disk(self):
        response = transport.FixtureTransport(FIXTURE).get("https://technocore.chat/rooms")
        with open(FIXTURE, "rb") as handle:
            raw = handle.read()
        self.assertTrue(response.ok)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.raw_body, raw)

    def test_fixture_source_names_the_file(self):
        self.assertEqual(
            transport.FixtureTransport(FIXTURE).source, "fixture:" + FIXTURE
        )


class FixtureReplayTests(unittest.TestCase):
    def test_a_replayed_status_is_not_ok_and_carries_an_error(self):
        response = transport.FixtureTransport(FIXTURE, status=503).get("u")
        self.assertFalse(response.ok)
        self.assertEqual(response.status, 503)
        self.assertIn("503", response.error)

    def test_replayed_headers_come_back(self):
        response = transport.FixtureTransport(
            FIXTURE, status=429, headers={"retry-after": "600"}
        ).get("u")
        self.assertEqual(response.headers["retry-after"], "600")

    def test_a_missing_fixture_is_reported_not_raised(self):
        response = transport.FixtureTransport("fixtures/does-not-exist.txt").get("u")
        self.assertIsNone(response.status)
        self.assertIsNone(response.raw_body)
        self.assertIn("fixture read failed", response.error)


class UserAgentTests(unittest.TestCase):
    def test_the_user_agent_identifies_the_tool_and_its_budget(self):
        agent = transport.USER_AGENT
        self.assertIn("technocore-observatory", agent)
        self.assertIn("30/hour", agent)


if __name__ == "__main__":
    unittest.main()
