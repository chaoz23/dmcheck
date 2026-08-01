"""Fail CI when pytest silently stops collecting part of the suite."""

import sys
from pathlib import Path

import pytest


EXPECTED_TESTS = 39
ROOT = Path(__file__).resolve().parent.parent


class _CollectionRecorder:
    count = 0

    def pytest_collection_finish(self, session):
        self.count = len(session.items)


def main():
    sys.path.insert(0, str(ROOT))
    recorder = _CollectionRecorder()
    result = pytest.main(["--collect-only", "-q"], plugins=[recorder])
    if result != pytest.ExitCode.OK:
        return int(result)
    if recorder.count != EXPECTED_TESTS:
        print(
            f"expected {EXPECTED_TESTS} tests, collected {recorder.count}; "
            "update the ratchet only after reviewing the collection diff",
            file=sys.stderr,
        )
        return 1
    print(f"collection contract satisfied: {recorder.count} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
