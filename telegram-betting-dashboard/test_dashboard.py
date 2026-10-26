import asyncio
import json
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from dashboard import DashboardServer


class DummyRunner:
    async def dashboard_state(self):
        return {"mode": "simulation", "paused": False}

    async def dashboard_action(self, action, payload):
        return {"action": action, "value": payload.get("value")}


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_serves_state_and_protected_actions(self):
        with TemporaryDirectory() as directory:
            web_root = Path(directory)
            (web_root / "index.html").write_text("dashboard", encoding="utf-8")
            server = DashboardServer(
                DummyRunner(), asyncio.get_running_loop(), web_root, port=0
            )
            url = server.start()
            try:
                def fetch_json(path):
                    with urllib.request.urlopen(url + path, timeout=3) as response:
                        return json.load(response)

                bootstrap = await asyncio.to_thread(fetch_json, "api/bootstrap")
                state = await asyncio.to_thread(fetch_json, "api/dashboard")
                self.assertTrue(bootstrap["token"])
                self.assertEqual(state["data"]["mode"], "simulation")

                def post_action():
                    request = urllib.request.Request(
                        url + "api/action",
                        data=json.dumps({"action": "pause", "value": 1}).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Local-Token": bootstrap["token"],
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return json.load(response)

                action = await asyncio.to_thread(post_action)
                self.assertEqual(action["data"]["action"], "pause")
            finally:
                await asyncio.to_thread(server.stop)


if __name__ == "__main__":
    unittest.main()
