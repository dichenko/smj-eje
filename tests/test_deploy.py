"""Exercise deployment with real temporary Git repos and simulated system services."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux deployment script")
class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.app = root / "app"
        self.units = root / "units"
        self.state = root / "state"
        self.mock_bin = root / "bin"
        self.log = root / "commands.log"
        for directory in (self.source, self.units, self.mock_bin):
            directory.mkdir()
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.email", "test@example.test")
        self.git(self.source, "config", "user.name", "Test")
        (self.source / ".gitignore").write_text(".venv/\n")
        (self.source / "requirements.txt").write_text("old\n")
        (self.source / "deploy").mkdir()
        for name in ("smj-web.service", "smj-sync.service", "smj-backup.service",
                     "smj-sync.timer", "smj-backup.timer"):
            (self.source / "deploy" / name).write_text("old unit\n")
            (self.units / name).write_text("old unit\n")
        self.commit("initial")
        self.previous = self.git(self.source, "rev-parse", "HEAD").strip()
        subprocess.run(["git", "clone", str(self.source), str(self.app)], check=True,
                       capture_output=True)
        (self.app / ".venv").mkdir()
        (self.source / ".gitignore").write_text(".venv\n")
        (self.source / "requirements.txt").write_text("new\n")
        (self.source / "deploy" / "smj-web.service").write_text("new unit\n")
        self.commit("update")
        self.target = self.git(self.source, "rev-parse", "HEAD").strip()
        script = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
        self.script = root / "deploy.sh"
        # Only remove root enforcement and redirect unit writes for the sandbox.
        content = script.read_text().replace(
            "[[ $EUID == 0 ]] || { echo 'Deployment requires root.' >&2; exit 1; }", "true")
        self.script.write_text(content.replace("/etc/systemd/system", str(self.units)))
        self.mock("systemctl", 'printf "%s\\n" "systemctl $*" >> "$TEST_LOG"\n')
        self.mock("curl", '''printf '%s\n' 'health check' >> "$TEST_LOG"
if [[ ${FAIL_HEALTH:-0} == 1 && ! -f "$TEST_LOG.health" ]]; then
    touch "$TEST_LOG.health"
    exit 1
fi
''')
        self.mock("python3", '''mkdir -p "$3/bin"
cat > "$3/bin/python" <<'PYTHON'
#!/usr/bin/env bash
echo 'pip check/install' >> "$TEST_LOG"
[[ ${FAIL_INSTALL:-0} != 1 ]]
PYTHON
chmod +x "$3/bin/python"
''')
        self.env = {**os.environ, "PATH": f"{self.mock_bin}:{os.environ['PATH']}",
                    "SMJ_APP_DIR": str(self.app), "SMJ_DEPLOY_STATE_DIR": str(self.state),
                    "TEST_LOG": str(self.log)}

    def mock(self, name, body):
        path = self.mock_bin / name
        path.write_text("#!/usr/bin/env bash\nset -e\n" + body)
        path.chmod(0o755)

    def git(self, directory, *arguments):
        return subprocess.run(["git", "-C", str(directory), *arguments], check=True,
                              capture_output=True, text=True).stdout

    def commit(self, message):
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", message)

    def deploy(self, **environment):
        return subprocess.run(["bash", str(self.script), self.target], capture_output=True,
                              text=True, env={**self.env, **environment})

    def assert_rollback(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.app, "rev-parse", "HEAD").strip(), self.previous)
        self.assertEqual((self.units / "smj-web.service").read_text(), "old unit\n")
        log = self.log.read_text()
        self.assertIn("systemctl restart smj-web.service", log)
        self.assertIn("systemctl start smj-sync.timer", log)
        self.assertIn("systemctl start smj-backup.timer", log)

    def test_success_installs_commit_and_restores_timers(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.app, "rev-parse", "HEAD").strip(), self.target)
        self.assertTrue((self.app / ".venv").is_symlink())
        self.assertEqual(self.git(self.app, "status", "--porcelain").strip(), "")
        for directory in (self.state, self.state / "venvs", (self.app / ".venv").resolve()):
            self.assertEqual(directory.stat().st_mode & 0o005, 0o005,
                             "smj service must be able to read and traverse the environment")
        self.assertEqual((self.app / "requirements.txt").stat().st_mode & 0o004, 0o004)
        self.assertEqual((self.units / "smj-web.service").read_text(), "new unit\n")
        log = self.log.read_text()
        self.assertLess(log.index("start smj-backup.service"), log.index("pip check/install"))
        self.assertLess(log.index("health check"), log.index("start smj-sync.timer"))

    def test_dependency_failure_restores_previous_code_and_services(self):
        self.assert_rollback(self.deploy(FAIL_INSTALL="1"))
        self.assertFalse((self.app / ".venv").is_symlink())

    def test_health_failure_restores_previous_environment(self):
        self.assert_rollback(self.deploy(FAIL_HEALTH="1"))
        self.assertEqual((self.app / ".venv").resolve(), self.state / "bootstrap-venv")

    def test_superseded_commit_never_stops_services(self):
        (self.source / "requirements.txt").write_text("newer\n")
        self.commit("newer push")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("superseded", result.stdout)
        self.assertEqual(self.git(self.app, "rev-parse", "HEAD").strip(), self.previous)
        self.assertFalse(self.log.exists())

    def test_local_changes_are_preserved(self):
        (self.app / "requirements.txt").write_text("local modification\n")
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / "requirements.txt").read_text(), "local modification\n")
        self.assertFalse(self.log.exists())
