"""Built artifacts must contain and implement the reviewed source contract.

dmcheck 0.5.1-0.5.4 shipped wheels without `dmcheck/dm_core.md`, because
package-data listed only `default_charter.json`. `dmcheck init --dm` therefore
raised an unhandled FileNotFoundError for every installed user, while working
perfectly in a checkout — the blind spot a cold-install test exists to close.

Found 2026-08-01 by the srdcheck post-mortem's cross-repo sweep: the identical
defect class had shipped in two separate packages.

The suite builds from a pristine copy, compares wheel and sdist payloads to the
reviewed tree, cold-runs the wheel, and executes the extracted sdist's included
tests. Artifact tests require the repository's test/build tooling; they never
skip merely because CI forgot to install it.
"""
import email
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import unittest
import zipfile

from dmcheck.core import public_charter
from dmcheck.validation import normalize_charter

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
SDIST_SELFTEST = os.environ.get("DMCHECK_SDIST_SELFTEST") == "1"


def _package_data_entries():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[tool\.setuptools\.package-data\](.*?)(\n\[|\Z)",
                      text, re.S)
    if not block:
        return set()
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def _build_distributions(outdir):
    """Build wheel and sdist from a pristine copy of the tree.

    setuptools reuses a stale build/lib/, so a wheel built where a file once
    lived keeps shipping it after package-data stops listing it. Copying to a
    clean tree reproduces what a fresh clone and CI actually produce.
    """
    src = pathlib.Path(outdir) / "src"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        "build", "dist", "*.egg-info", "__pycache__", ".git", ".venv"))
    dist = pathlib.Path(outdir) / "dist"
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel",
         "--no-isolation", "--outdir", str(dist)],
        cwd=src, capture_output=True, text=True)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if proc.returncode or len(wheels) != 1 or len(sdists) != 1:
        raise AssertionError(
            "artifact build failed or produced an unexpected archive set\n"
            f"command exit: {proc.returncode}\nstdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}\nfiles: "
            f"{sorted(p.name for p in dist.glob('*')) if dist.exists() else []}")
    return wheels[0], sdists[0]


def _wheel_files(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()
                if not name.endswith("/")}


def _sdist_files(path):
    with tarfile.open(path, "r:gz") as archive:
        files = {}
        for member in archive.getmembers():
            if not member.isfile() or "/" not in member.name:
                continue
            relative = member.name.split("/", 1)[1]
            extracted = archive.extractfile(member)
            if extracted is not None:
                files[relative] = extracted.read()
        return files


def _reviewed_package_files():
    return {
        p.relative_to(ROOT).as_posix(): p.read_bytes()
        for p in PKG.rglob("*")
        if p.is_file() and p.suffix != ".pyc"
    }


def _required_sdist_test_files():
    tests = ROOT / "tests"
    paths = sorted(tests.glob("test_*.py"))
    paths.extend(sorted((tests / "fixtures").rglob("*")))
    return {
        p.relative_to(ROOT).as_posix(): p.read_bytes()
        for p in paths if p.is_file()
    }


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


@unittest.skipIf(SDIST_SELFTEST, "avoid recursively rebuilding artifacts")
class TestBuiltArtifacts(unittest.TestCase):
    """Builds and exercises the exact archives a release would publish."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="dmcheck-pkg-")
        cls.wheel, cls.sdist = _build_distributions(cls._tmp)
        cls.wheel_payload = _wheel_files(cls.wheel)
        cls.sdist_payload = _sdist_files(cls.sdist)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_wheel_contains_runtime_data(self):
        for name in RUNTIME_DATA:
            self.assertTrue(
                any(n.endswith(f"dmcheck/{name}")
                    for n in self.wheel_payload),
                f"wheel omits {name} — installed users will crash")

    def test_wheel_and_sdist_package_payloads_match_source(self):
        for relative, expected in _reviewed_package_files().items():
            self.assertIn(relative, self.wheel_payload,
                          f"wheel omits reviewed source file {relative}")
            self.assertEqual(self.wheel_payload[relative], expected,
                             f"wheel content drifted for {relative}")
            self.assertIn(relative, self.sdist_payload,
                          f"sdist omits reviewed source file {relative}")
            self.assertEqual(self.sdist_payload[relative], expected,
                             f"sdist content drifted for {relative}")

    def test_sdist_contains_tests_and_fixture_bytes(self):
        for relative, expected in _required_sdist_test_files().items():
            self.assertIn(relative, self.sdist_payload,
                          f"sdist omits release self-test input {relative}")
            self.assertEqual(self.sdist_payload[relative], expected,
                             f"sdist test input drifted for {relative}")

    def test_wheel_uses_pep639_license_metadata(self):
        metadata_names = [name for name in self.wheel_payload
                          if name.endswith(".dist-info/METADATA")]
        self.assertEqual(len(metadata_names), 1)
        metadata = email.message_from_bytes(
            self.wheel_payload[metadata_names[0]])
        self.assertEqual(metadata.get("License-Expression"), "MIT")
        self.assertEqual(set(metadata.get_all("License-File", [])),
                         {"LICENSE", "NOTICE"})
        self.assertIsNone(metadata.get("License"),
                          "legacy free-text License metadata must not return")

    def test_cold_installed_wheel_serves_init_dm(self):
        """The regression gate for 0.5.1-0.5.4: install into an empty
        environment and run `init --dm` from outside the repo."""
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
        checkout = public_charter(normalize_charter(json.loads(
            (PKG / "default_charter.json").read_text(encoding="utf-8"))))
        self.assertEqual(installed, checkout)

    def test_cold_installed_wheel_runs_table_event_adapter(self):
        """The wheel, not the checkout, must expose the fail-closed adapter."""
        venv = pathlib.Path(self._tmp) / "event-venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        win = sysconfig.get_platform().startswith("win")
        bindir = venv / ("Scripts" if win else "bin")
        pip = bindir / ("pip.exe" if win else "pip")
        dmcheck = bindir / ("dmcheck.exe" if win else "dmcheck")
        subprocess.run([str(pip), "install", "-q", str(self.wheel)], check=True)
        elsewhere = pathlib.Path(self._tmp) / "event-elsewhere"
        elsewhere.mkdir(exist_ok=True)
        event = {
            "schema_version": "table.event/1.0", "event_id": "evt-gap",
            "campaign_id": "campaign", "session_id": "session",
            "session_sequence": 1,
            "source": {"kind": "fixture", "instance": "cold-wheel",
                       "native_id": "1", "sequence": 1,
                       "attestation": "self_attested"},
            "occurred_at": "2026-08-01T19:00:00Z",
            "recorded_at": "2026-08-01T19:00:00Z",
            "principal": {"id": "transport", "actor_id": None,
                          "controller_id": None, "role": "system"},
            "event_type": "transport.gap",
            "payload": {"expected_sequence": 7, "observed_sequence": 8,
                        "recoverable": False},
            "correlation_ids": [], "causation_id": None,
            "audience": ["operator"], "visibility": "system",
            "sensitivity": "normal", "provenance": "observed",
            "integrity": {"predecessor_digest": None, "event_digest": None,
                          "checkpoint": "same_writer"},
        }
        stream = elsewhere / "golden-gap.jsonl"
        stream.write_text(json.dumps(event) + "\n", encoding="utf-8")
        proc = subprocess.run(
            [str(dmcheck), "run-events", str(stream), "--gm", "gm-dan"],
            cwd=elsewhere, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("table_event.transport_gap",
                      {item["code"] for item in result["errors"]})
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_cold_installed_server_json_entrypoint_discovers(self):
        """Install the candidate wheel, then launch the console script named
        by server.json from a directory that contains no source checkout.

        Public registry resolution is a separate post-publication gate: 0.6.0
        is deliberately not available from PyPI while this test runs.
        """
        if not self.wheel:
            self.skipTest("wheel build unavailable (needs `build`+`setuptools`)")
        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        package = manifest["packages"][0]
        executable_name = [
            argument["value"] for argument in package["runtimeArguments"]
            if argument.get("type") == "positional"
        ][-1]

        venv = pathlib.Path(self._tmp) / "mcp-venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        win = sysconfig.get_platform().startswith("win")
        bindir = venv / ("Scripts" if win else "bin")
        pip = bindir / ("pip.exe" if win else "pip")
        executable = bindir / (executable_name + (".exe" if win else ""))
        subprocess.run([str(pip), "install", "-q", str(self.wheel)], check=True)

        elsewhere = pathlib.Path(self._tmp) / "mcp-elsewhere"
        elsewhere.mkdir(exist_ok=True)
        request = {
            "jsonrpc": "2.0", "id": "cold", "method": "server/discover",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "cold-smoke", "version": "1.0.0"
                },
            }},
        }
        proc = subprocess.run(
            [str(executable)], cwd=elsewhere,
            input=json.dumps(request) + "\n", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        response = json.loads(proc.stdout)
        self.assertEqual(response["id"], "cold")
        self.assertEqual(response["result"]["supportedVersions"],
                         ["2026-07-28"])
        self.assertEqual(
            response["result"]["_meta"][
                "io.modelcontextprotocol/serverInfo"]["version"],
            manifest["version"])

    def test_extracted_sdist_passes_its_included_suite(self):
        extracted = pathlib.Path(self._tmp) / "sdist"
        extracted.mkdir()
        for relative, content in self.sdist_payload.items():
            archive_path = pathlib.PurePosixPath(relative)
            self.assertFalse(archive_path.is_absolute())
            self.assertNotIn("..", archive_path.parts)
            target = extracted.joinpath(*archive_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        env = os.environ.copy()
        env["DMCHECK_SDIST_SELFTEST"] = "1"
        env.pop("PYTHONPATH", None)
        # The sdist self-test must not accidentally rely on a globally
        # installed third-party runner. Every shipped test is unittest-based.
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=extracted, env=env, capture_output=True, text=True)
        self.assertEqual(
            proc.returncode, 0,
            f"extracted sdist suite failed ({proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
