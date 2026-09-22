#!/usr/bin/env python3
"""check_deps.py — fail when the venv is missing something the installed
custom nodes or this repo's pin files need.

Written after a real miss: nvidia-ml-py was left out of the venv, so
Crystools' GPU monitor reported an empty GPU list and the resource watcher
logged no GPU or VRAM stats. Nothing crashed and no test failed — the
feature just quietly did nothing. `pip check` had been saying
"jetson-stats requires nvidia-ml-py, which is not installed" the whole
time.

Three sources, because each catches what the others miss:
  1. every custom_nodes/*/requirements.txt, with environment markers
     evaluated (on aarch64 Crystools needs jetson-stats, not pynvml), so a
     node's own declared needs are checked even when this repo never
     pinned them;
  2. this repo's requirements/custom-nodes.txt and requirements/test.txt,
     so the venv still matches what it claims to be;
  3. `pip check`, which catches an installed package whose own dependency
     is missing or out of range.

Usage:
    check_deps.py [--repo-root DIR] [--skip-test-requirements]

Exit code 0 when everything is satisfied, 1 otherwise. Problems are
printed one per line to stderr.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

from packaging.requirements import Requirement

# `pip check` lines that are expected here and must not fail the gate. The
# tuned torch build carries a local version (2.15.0+gb10...) that no
# published torchvision declares, so torchvision's metadata always
# disagrees; torchvision is verified to work against it in practice (see
# the torchvision/torchaudio GB10 compatibility note).
_PIP_CHECK_ALLOWED = ("torchvision", "has requirement torch==")

# Requirements deliberately left unsatisfied, as "<node>:<package>" with the
# reason. Only add an entry that has been checked on the hardware; the point of
# this script is to catch the ones nobody checked.
_ALLOWED_UNMET = {
    # quanto's get_max_cuda_arch() does int(arch.split("_")[1]) over
    # torch.cuda.get_arch_list(); the tuned GB10 build reports "sm_121a", so
    # importing it raises ValueError: invalid literal for int(): '121a'.
    # Worse than absent: transformers' is_optimum_quanto_available() reads
    # metadata, not the import, so with it installed the libraries take the
    # fp8 quanto path and crash mid-run. Cosmos3's nf4/int8 paths
    # (bitsandbytes) are unaffected.
    "scg-Cosmos3-tuned:optimum-quanto": "breaks on sm_121a (GB10)",
    # ltxvideo pins ninja~=1.11.1.4, which this venv has never satisfied; the
    # LTX-2.5 workflow generates successfully on the installed ninja, checked
    # by the integration test in the publish gate.
    "comfyui-ltxvideo:ninja": "pin is over-tight; verified working",
}


class DependencyChecker:
    """Check the running interpreter's environment against what is declared."""

    def __init__(self, repo_root: Path, check_test_requirements: bool = True) -> None:
        """Set the checkout to inspect and whether test-only pins count."""
        self._repo_root = repo_root
        self._check_test_requirements = check_test_requirements
        self._problems: list[str] = []

    def run(self) -> int:
        """Run every check and return the process exit code."""
        self._check_custom_node_requirements()
        self._check_pinned_requirements()
        self._check_pip_check()

        if self._problems:
            print(
                f"Dependency check FAILED ({len(self._problems)} problem(s)):",
                file=sys.stderr,
            )
            for problem in self._problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        print("Dependency check OK")
        return 0

    def _check_custom_node_requirements(self) -> None:
        """Check each installed custom node's own requirements.txt."""
        custom_nodes = self._repo_root / "custom_nodes"
        if not custom_nodes.is_dir():
            return
        for requirements in sorted(custom_nodes.glob("*/requirements.txt")):
            node = requirements.parent.name
            for requirement in self._parse(requirements):
                unmet = self._unmet(requirement)
                if unmet is None:
                    continue
                reason = _ALLOWED_UNMET.get(f"{node}:{requirement.name}")
                if reason:
                    print(f"  allowed: {node} {requirement.name} ({reason})")
                    continue
                self._problems.append(f"{node} needs {requirement}: {unmet}")

    def _check_pinned_requirements(self) -> None:
        """Check this repo's own pin files still describe the venv."""
        names = ["custom-nodes.txt"]
        if self._check_test_requirements:
            names.append("test.txt")
        for name in names:
            path = self._repo_root / "comfyui" / "zbrad" / "requirements" / name
            if not path.is_file():
                continue
            for requirement in self._parse(path):
                unmet = self._unmet(requirement)
                if unmet:
                    self._problems.append(f"{name} pins {requirement}: {unmet}")

    def _check_pip_check(self) -> None:
        """Run `pip check`, ignoring the known-acceptable lines."""
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("No broken requirements"):
                continue
            if all(part in line for part in _PIP_CHECK_ALLOWED):
                continue
            self._problems.append(f"pip check: {line}")

    @staticmethod
    def _parse(path: Path) -> list[Requirement]:
        """Parse a requirements file, keeping only what applies here."""
        requirements = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            try:
                requirement = Requirement(line)
            except Exception:
                continue
            # An environment marker decides whether this line applies at all:
            # `pynvml; platform_machine != 'aarch64'` must be skipped here.
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            requirements.append(requirement)
        return requirements

    @staticmethod
    def _unmet(requirement: Requirement) -> Optional[str]:
        """Return why the requirement is unmet, or None when it is met."""
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            return "not installed"
        if requirement.specifier and not requirement.specifier.contains(
            installed, prereleases=True
        ):
            return f"installed {installed}"
        return None

    @staticmethod
    def _build_arg_parser() -> argparse.ArgumentParser:
        """Build the command-line parser."""
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--repo-root",
            type=Path,
            default=Path(__file__).resolve().parents[3],
            help="checkout to inspect (default: the one holding this script)",
        )
        parser.add_argument(
            "--skip-test-requirements",
            action="store_true",
            help="do not require the test-only pins (requirements/test.txt)",
        )
        return parser

    @classmethod
    def main(cls, argv: Optional[list[str]] = None) -> int:
        """Entry point."""
        args = cls._build_arg_parser().parse_args(argv)
        checker = cls(
            repo_root=args.repo_root,
            check_test_requirements=not args.skip_test_requirements,
        )
        return checker.run()


if __name__ == "__main__":
    sys.exit(DependencyChecker.main())
