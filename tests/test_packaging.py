"""The installed artifact must implement the advertised behaviour.

dmcheck 0.5.1-0.5.4 shipped wheels without `dmcheck/dm_core.md`, because
package-data listed only `default_charter.json`. `dmcheck init --dm` therefore
raised an unhandled FileNotFoundError for every installed user, while working
perfectly in a checkout — the blind spot a cold-install test exists to close.

Found 2026-08-01 by the srdcheck post-mortem's cross-repo sweep: the identical
defect class had shipped in two separate packages.

Written as stdlib unittest, not pytest, so it actually runs under this repo's
`python -m unittest discover` CI. A gate that CI does not execute is decorative.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "dmcheck"

# Every non-.py file the package reads at runtime.
RUNTIME_DATA = [
    "default_charter.json",
    "charter.schema.json",
    "transcript.schema.json",
    "ledger.schema.json",
    "evaluation-result.schema.json",
    "dm_core.md",
]


def _package_data_entries():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[tool\.setuptools\.package-data\](.*?)(\n\[|\Z)",
                      text, re.S)
    if not block:
        return set()
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def _build_wheel(outdir):
    """Build from a pristine copy of the tree.

    setuptools reuses a stale build/lib/, so a wheel built where a file once
    lived keeps shipping it after package-data stops listing it. Copying to a
    clean tree reproduces what a fresh clone and CI actually produce.
    """
    src = pathlib.Path(outdir) / "src"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        "build", "dist", "*.egg-info", "__pycache__", ".git", ".venv"))
    wheelhouse = pathlib.Path(outdir) / "wheelhouse"
    attempts = [
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(wheelhouse)],
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheelhouse)],
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
         "--no-build-isolation", "--wheel-dir", str(wheelhouse)],
    ]
    for cmd in attempts:
        proc = subprocess.run(cmd, cwd=src, capture_output=True, text=True)
        if proc.returncode == 0:
            wheels = list(wheelhouse.glob("dmcheck-*.whl"))
            if wheels:
                return wheels[0]
    return None


class TestPackagingContract(unittest.TestCase):
    """Fast structural checks — no build toolchain required."""

    def test_runtime_data_files_exist(self):
        for name in RUNTIME_DATA:
            self.assertTrue((PKG / name).is_file(),
                            f"missing source data file: {name}")

    def test_package_data_lists_every_runtime_data_file(self):
        """The check that would have caught 0.5.1.

        Parses the package-data list specifically; a substring search over the
        whole file is not enough, since the explanatory comment above the list
        also names dm_core.md.
        """
        entries = _package_data_entries()
        self.assertTrue(entries, "no [tool.setuptools.package-data] section")
        for name in RUNTIME_DATA:
            self.assertIn(
                name, entries,
                f"{name} is read at runtime but absent from package-data "
                f"(declared: {sorted(entries)}); it will be missing from the "
                f"wheel and installed users will hit a crash")

    def test_no_undeclared_runtime_data(self):
        """A new data file must force a packaging decision, not silently miss
        the wheel."""
        found = {p.name for p in PKG.iterdir()
                 if p.is_file() and p.suffix not in (".py", ".pyc")}
        undeclared = found - set(RUNTIME_DATA)
        self.assertFalse(
            undeclared,
            f"new data file(s) {undeclared} in the package: add them to "
            f"package-data AND to RUNTIME_DATA in this test")

    def test_init_dm_fails_legibly_when_data_missing(self):
        """Even with the packaging bug present, the failure must be an honest
        machine-readable error, never a traceback at someone setting up a
        table. dmcheck's whole contract is legible verdicts."""
        src = (ROOT / "dmcheck" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("packaging-incomplete", src)

    def test_only_packaged_default_is_authoritative(self):
        self.assertFalse((ROOT / "charters" / "default.json").exists())
        from dmcheck.validation import canonical_charter_digest
        value = json.loads((PKG / "default_charter.json").read_text(encoding="utf-8"))
        self.assertEqual(value["charter_digest"],
                         canonical_charter_digest(value))
        self.assertEqual(value["schema_version"], "1.0")


class TestInstalledArtifact(unittest.TestCase):
    """Builds a wheel; skips cleanly where no build toolchain exists."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="dmcheck-pkg-")
        cls.wheel = _build_wheel(cls._tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_wheel_contains_runtime_data(self):
        if not self.wheel:
            self.skipTest("wheel build unavailable (needs `build`+`setuptools`)")
        names = zipfile.ZipFile(self.wheel).namelist()
        for name in RUNTIME_DATA:
            self.assertTrue(
                any(n.endswith(f"dmcheck/{name}") for n in names),
                f"wheel omits {name} — installed users will crash")

    def test_cold_installed_wheel_serves_init_dm(self):
        """The regression gate for 0.5.1-0.5.4: install into an empty
        environment and run `init --dm` from outside the repo."""
        if not self.wheel:
            self.skipTest("wheel build unavailable (needs `build`+`setuptools`)")
        venv = pathlib.Path(self._tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        win = sysconfig.get_platform().startswith("win")
        bindir = venv / ("Scripts" if win else "bin")
        pip = bindir / ("pip.exe" if win else "pip")
        dmcheck = bindir / ("dmcheck.exe" if win else "dmcheck")
        subprocess.run([str(pip), "install", "-q", str(self.wheel)], check=True)

        workdir = pathlib.Path(self._tmp) / "elsewhere"
        workdir.mkdir(exist_ok=True)
        proc = subprocess.run([str(dmcheck), "init", "--dm"],
                              cwd=workdir, capture_output=True, text=True)
        self.assertEqual(
            proc.returncode, 0,
            f"installed `init --dm` failed ({proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}")
        core = workdir / "DM-CORE.md"
        self.assertTrue(core.is_file(), "DM-CORE.md was not written")
        self.assertTrue(core.read_text().strip(), "DM-CORE.md is empty")
        json.loads(proc.stdout)  # output stays machine-readable

    def test_cold_installed_default_matches_checkout(self):
        if not self.wheel:
            self.skipTest("wheel build unavailable (needs `build`+`setuptools`)")
        venv = pathlib.Path(self._tmp) / "parity-venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        win = sysconfig.get_platform().startswith("win")
        bindir = venv / ("Scripts" if win else "bin")
        pip = bindir / ("pip.exe" if win else "pip")
        dmcheck = bindir / ("dmcheck.exe" if win else "dmcheck")
        subprocess.run([str(pip), "install", "-q", str(self.wheel)], check=True)
        elsewhere = pathlib.Path(self._tmp) / "parity-elsewhere"
        elsewhere.mkdir(exist_ok=True)
        proc = subprocess.run([str(dmcheck), "charter"], cwd=elsewhere,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        installed = json.loads(proc.stdout)
        checkout = json.loads((PKG / "default_charter.json").read_text(
            encoding="utf-8"))
        self.assertEqual(installed, checkout)


if __name__ == "__main__":
    unittest.main()
