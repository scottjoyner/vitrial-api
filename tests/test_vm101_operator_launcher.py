"""Guard the operator-only x1-370 launcher's exact-head and HOLD behavior."""
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
WRAPPER=ROOT/"scripts"/"run_vm101_readonly_operator.sh"

class LauncherTests(unittest.TestCase):
    def test_shell_parses(self):
        result=subprocess.run(["bash","-n",str(WRAPPER)],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0)

    def test_missing_or_unreviewed_sha_never_connects_to_fleet(self):
        for arguments in ([],["invalid"],["f"*40]):
            with self.subTest(arguments=arguments):
                result=subprocess.run(["bash",str(WRAPPER),*arguments],
                                      capture_output=True,text=True,timeout=6)
                self.assertEqual(result.returncode,2)
                self.assertNotIn("PROBE_SUMMARY",result.stdout)

    def test_fixed_readonly_runtime_and_explicit_denial(self):
        s=WRAPPER.read_text()
        self.assertIn("git -C \"$repo\" status --porcelain",s)
        self.assertIn("--proxmox-host root@38.143.56.138",s)
        self.assertIn("--output \"$report\"",s)
        self.assertIn("evidence_window_exceeded",s)
        self.assertIn('exit 2',s)
        for mutation in ("qmrestore ","qm create","qm start","pg_ctlcluster",
                         "docker compose up","systemctl restart","rsync "):
            self.assertNotIn(mutation,s)

if __name__=="__main__":
    unittest.main()
