"""Repository-native validation command detection for guided setup."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Mapping

from ..ports.repository_setup import RepositorySetupValidationDefaults

_MAKE_QUICK_TARGETS = (
    "validate-fast",
    "test-quick",
    "test-unit",
    "test",
    "check",
    "validate",
)
_MAKE_PUBLISH_TARGETS = (
    "validate-pr-raw",
    "validate",
    "test",
    "check",
)


class RepositorySetupValidationDetectorAdapter:
    """Infer executable validation gates from repository-owned project files."""

    def __call__(self, repo_root: Path) -> RepositorySetupValidationDefaults:
        make_defaults = self._from_makefile(repo_root)
        if make_defaults is not None:
            return make_defaults

        package_defaults = self._from_package_json(repo_root)
        if package_defaults is not None:
            return package_defaults

        for marker, command, source in (
            ("Cargo.toml", "cargo test", "Cargo.toml"),
            ("go.mod", "go test ./...", "go.mod"),
            ("gradlew", "./gradlew test", "Gradle wrapper"),
            ("mvnw", "./mvnw test", "Maven wrapper"),
            ("pom.xml", "mvn test", "pom.xml"),
        ):
            if (repo_root / marker).is_file():
                return RepositorySetupValidationDefaults(command, command, source)

        if (repo_root / "tests").is_dir() and any(
            (repo_root / marker).is_file()
            for marker in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
        ):
            return RepositorySetupValidationDefaults(
                "python -m pytest -q",
                "python -m pytest",
                "Python test configuration",
            )

        return RepositorySetupValidationDefaults(
            None,
            None,
            (
                "No repository-native validation command was detected. "
                "Enter quick and publish commands before continuing."
            ),
        )

    @staticmethod
    def _from_makefile(repo_root: Path) -> RepositorySetupValidationDefaults | None:
        makefile = next(
            (
                repo_root / name
                for name in ("GNUmakefile", "Makefile", "makefile")
                if (repo_root / name).is_file()
            ),
            None,
        )
        if makefile is None:
            return None
        try:
            source = makefile.read_text(errors="replace")
        except OSError:
            return None
        targets = {
            match.group(1)
            for match in re.finditer(
                r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(?:\s|$)",
                source,
                flags=re.MULTILINE,
            )
        }
        quick = next((target for target in _MAKE_QUICK_TARGETS if target in targets), None)
        publish = next(
            (target for target in _MAKE_PUBLISH_TARGETS if target in targets),
            None,
        )
        if quick is None or publish is None:
            return None
        return RepositorySetupValidationDefaults(
            f"make {quick}",
            f"make {publish}",
            f"{makefile.name} targets",
        )

    @staticmethod
    def _from_package_json(
        repo_root: Path,
    ) -> RepositorySetupValidationDefaults | None:
        package_json = repo_root / "package.json"
        if not package_json.is_file():
            return None
        try:
            raw = json.loads(package_json.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        scripts = raw.get("scripts") if isinstance(raw, Mapping) else None
        if not isinstance(scripts, Mapping) or "test" not in scripts:
            return None

        if (repo_root / "pnpm-lock.yaml").is_file():
            runner = "pnpm"
        elif (repo_root / "yarn.lock").is_file():
            runner = "yarn"
        else:
            runner = "npm"
        quick = f"{runner} test"
        publish_script = next(
            (name for name in ("validate", "ci", "test") if name in scripts),
            "test",
        )
        publish = (
            f"{runner} test"
            if publish_script == "test"
            else f"{runner} run {publish_script}"
        )
        return RepositorySetupValidationDefaults(
            quick,
            publish,
            "package.json scripts",
        )


__all__ = ["RepositorySetupValidationDetectorAdapter"]
