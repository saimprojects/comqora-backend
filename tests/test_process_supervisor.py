import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

import deploy


class CombinedProcessTests(unittest.TestCase):
    @patch("deploy.os.killpg", create=True)
    @patch("deploy.signal.signal")
    @patch("deploy.subprocess.Popen")
    def test_either_service_exit_stops_peer_and_requests_restart(self, popen, signals, killpg):
        for failed in (0, 1):
            with self.subTest(failed=failed):
                children = [Mock(pid=101, returncode=0), Mock(pid=102, returncode=0)]
                for index, child in enumerate(children):
                    child.poll.return_value = 0 if index == failed else None
                popen.side_effect = children
                self.assertEqual(deploy.run_combined(), 1)
                for child in children:
                    child.wait.assert_called_once()
                self.assertEqual(popen.call_args.args[0][-2:], ["sync_tracking", "--loop"])

    @patch("deploy.os.killpg", create=True)
    @patch("deploy.signal.signal")
    @patch("deploy.subprocess.Popen")
    def test_shutdown_signal_stops_both_without_restart(self, popen, signals, killpg):
        children = [Mock(pid=101), Mock(pid=102)]

        def launch(*args, **kwargs):
            child = children[popen.call_count - 1]
            if popen.call_count == 2:
                signals.call_args_list[0].args[1](signal.SIGTERM, None)
            return child

        popen.side_effect = launch
        self.assertEqual(deploy.run_combined(), 0)
        for child in children:
            child.wait.assert_called_once()

    @patch("deploy.os.killpg", create=True)
    @patch("deploy.signal.signal")
    @patch("deploy.subprocess.Popen")
    def test_partial_start_failure_cleans_up_and_kills_stuck_process(self, popen, signals, killpg):
        child = Mock(pid=101)
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("gunicorn", 30), 0]
        popen.side_effect = [child, OSError("worker launch failed")]
        with self.assertRaises(OSError):
            deploy.run_combined()
        self.assertEqual(child.wait.call_count, 2)
        if deploy.os.name == "posix":
            killpg.assert_any_call(101, signal.SIGKILL)
        else:
            child.kill.assert_called_once()
