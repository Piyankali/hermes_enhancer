"""v0.22 installer tests: execute the real setup.sh/uninstall.sh.

Every test points HERMES_HOME at an isolated tmp_path fake home, so the
real ~/.hermes installation is never touched. SKIP_DISCOVERY=1 avoids
depending on the Hermes CLI; discovery mechanics were verified manually
(`hermes plugins list` scans $HERMES_HOME/plugins/<id>/).
"""
import os
import shutil
import stat
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(REPO, "setup.sh")
UNINSTALL = os.path.join(REPO, "uninstall.sh")

EXPECTED_FILES = {
    "__init__.py", "enhancer.py", "federated_db.py",
    "feedback_optimizer.py", "predictive_preload.py", "meta_learner.py",
    "skill_graph.py", "composer.py", "self_test.py",
    "plugin.yaml", "README.md",
}
FORBIDDEN = (".git", "__pycache__", ".pyc", ".pytest_cache", ".venv")


def run(script, home, **kwargs):
    env = dict(os.environ, HERMES_HOME=home, SKIP_DISCOVERY="1")
    return subprocess.run(
        ["bash", script], cwd=REPO, env=env,
        capture_output=True, text=True, timeout=300, **kwargs,
    )


def plugin_dir(home):
    return os.path.join(home, "plugins", "hermes_enhancer")


def installed_files(home):
    return set(os.listdir(plugin_dir(home)))


def test_a_fresh_install(tmp_path):
    home = str(tmp_path / "hermes_home")
    proc = run(SETUP, home)
    assert proc.returncode == 0, proc.stderr
    assert "Plugin installation: PASS" in proc.stdout
    assert "Plugin version: 0.22.0" in proc.stdout
    assert installed_files(home) == EXPECTED_FILES
    assert "version: 0.22.0" in open(
        os.path.join(plugin_dir(home), "plugin.yaml")).read()
    for bad in FORBIDDEN:
        assert not any(bad in f for f in installed_files(home)), bad


def test_b_repeated_install_idempotent(tmp_path):
    home = str(tmp_path / "hermes_home")
    for _ in range(3):
        proc = run(SETUP, home)
        assert proc.returncode == 0, proc.stderr
    assert installed_files(home) == EXPECTED_FILES
    nested = os.path.join(plugin_dir(home), "hermes_enhancer")
    assert not os.path.exists(nested), "nested plugin directory created"
    assert "version: 0.22.0" in open(
        os.path.join(plugin_dir(home), "plugin.yaml")).read()


def test_c_upgrade_from_old_version(tmp_path):
    home = str(tmp_path / "hermes_home")
    old = plugin_dir(home)
    os.makedirs(old)
    with open(os.path.join(old, "plugin.yaml"), "w") as fh:
        fh.write("name: hermes_enhancer\nversion: 0.20.7\n")
    with open(os.path.join(old, "federated_db.py"), "w") as fh:
        fh.write("# ancient\n")
    proc = run(SETUP, home)
    assert proc.returncode == 0, proc.stderr
    assert "updating safely" in proc.stdout
    assert installed_files(home) == EXPECTED_FILES
    assert "version: 0.22.0" in open(
        os.path.join(old, "plugin.yaml")).read()
    backups = [f for f in os.listdir(os.path.join(home, "plugins"))
               if f.startswith(".hermes_enhancer.bak.")]
    assert len(backups) == 1
    assert "version: 0.20.7" in open(
        os.path.join(home, "plugins", backups[0], "plugin.yaml")).read()


def test_d_uninstall_removes_only_enhancer(tmp_path):
    home = str(tmp_path / "hermes_home")
    other = os.path.join(home, "plugins", "some_other_plugin")
    os.makedirs(other)
    marker = os.path.join(other, "keep.me")
    with open(marker, "w") as fh:
        fh.write("untouched")
    assert run(SETUP, home).returncode == 0
    proc = run(UNINSTALL, home)
    assert proc.returncode == 0, proc.stderr
    assert not os.path.exists(plugin_dir(home))
    assert open(marker).read() == "untouched"


def test_e_uninstall_twice_safe(tmp_path):
    home = str(tmp_path / "hermes_home")
    assert run(SETUP, home).returncode == 0
    assert run(UNINSTALL, home).returncode == 0
    proc = run(UNINSTALL, home)
    assert proc.returncode == 0, proc.stderr
    assert "NOTHING TO REMOVE" in proc.stdout


def test_f_missing_required_file_fails_cleanly(tmp_path):
    slim = str(tmp_path / "slimrepo")
    os.makedirs(slim)
    shutil.copy(SETUP, slim)
    shutil.copy(os.path.join(REPO, "plugin.yaml"), slim)
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "hermes_home"),
               SKIP_DISCOVERY="1")
    proc = subprocess.run(
        ["bash", os.path.join(slim, "setup.sh")], cwd=slim, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0
    assert "ERROR" in proc.stderr
    assert not os.path.exists(
        os.path.join(env["HERMES_HOME"], "plugins", "hermes_enhancer")), (
        "broken installation must not be left behind")


def test_g_state_db_protection(tmp_path):
    home = str(tmp_path / "hermes_home")
    os.makedirs(home)
    state_db = os.path.join(home, "state.db")
    with open(state_db, "wb") as fh:
        fh.write(b"SENTINEL-CONTENT")
    before_stat = os.stat(state_db)
    time.sleep(0.05)

    assert run(SETUP, home).returncode == 0
    assert run(SETUP, home).returncode == 0
    assert run(UNINSTALL, home).returncode == 0
    assert run(UNINSTALL, home).returncode == 0

    with open(state_db, "rb") as fh:
        assert fh.read() == b"SENTINEL-CONTENT"
    after_stat = os.stat(state_db)
    assert (after_stat.st_mtime_ns == before_stat.st_mtime_ns
            and after_stat.st_size == before_stat.st_size)

    for script in (SETUP, UNINSTALL):
        text = open(script).read()
        code_lines = []
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("echo"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        assert "doctor" not in code, script
        assert "sqlite" not in code.lower(), script
        assert "state.db" not in code, script


def test_scripts_have_shebang_and_clean_syntax():
    import subprocess as sp
    for script in (SETUP, UNINSTALL):
        with open(script) as fh:
            assert fh.readline().strip() == "#!/usr/bin/env bash", script
        proc = sp.run(["bash", "-n", script], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    # NOTE: chmod +x is a no-op on this shared-storage mount (mode bits
    # ignored), so tests invoke the scripts via `bash <script>`. Set the
    # executable bit when packaging on a POSIX filesystem.
