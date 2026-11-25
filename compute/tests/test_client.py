import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from neurogrid_node import REVISION, RUNTIME
from neurogrid_node.client import GridError, run_node


class NodeClientTest(unittest.TestCase):
    def test_retries_startup_and_reports_rejected_result(self):
        stop = Event()
        report = Mock()

        def respond(action, _data=None):
            if action == "profile":
                if client.call.call_count == 1:
                    raise GridError("busy", 503)
                return {"id": "node", "revision": REVISION, "runtime": RUNTIME, "shard": 0}
            if action == "claim":
                return {"work": {"input": {}, "length": 1, "lease": "test", "job": "job",
                                 "epoch": 0, "position": 0, "emit": True}}
            if action == "result":
                stop.set()
                return {"accepted": False, "reason": "malformed_result"}
            return {"ok": True}

        client = Mock()
        client.call.side_effect = respond
        with (
            patch.object(stop, "wait", return_value=False),
            patch("neurogrid_node.model.Partition") as partition,
            patch("neurogrid_node.model.configure_cpu"),
        ):
            partition.return_value.index = 0
            partition.return_value.run.return_value = {"tensor": "fixture"}
            run_node(client, stop, model_path=Path("unused"), report=report)

        report.assert_any_call("Coordinator unavailable · retrying after busy")
        report.assert_any_call("Result rejected · malformed_result")
        self.assertEqual(client.call.call_args.args[0], "pause")


if __name__ == "__main__":
    unittest.main()
