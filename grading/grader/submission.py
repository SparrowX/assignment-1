"""Stage only assignment inputs, on top of the course starter for implementation tests."""

import ast
import json
import re
import stat
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STARTER = "67498d87e36f7b45a2a4ce82d65d1139fb6a51c0"
INSTANCE = "django__django-15368"
AB_FILE = re.compile(
    r"part3-(?:no-)?legal-moves-(?P<tag>[A-Za-z0-9][A-Za-z0-9_.-]*?)(?:-result)?\.json\Z"
)


def ab_name(model, legal):
    return f"part3-{'' if legal else 'no-'}legal-moves-{model}"


ARTIFACTS = {
    "fix.patch",
    "part1-trajectory.json",
    f"{INSTANCE}.patch",
    f"{INSTANCE}-trajectory.json",
    "token-usage-analysis.md",
    "part3-trajectory.json",
    "game-result.json",
    "observation-experiment.md",
    "part3-python-skill-trajectory.json",
}
REQUIRED = {"artifacts/" + name for name in ARTIFACTS} | {"AI_USAGE.md"}
CODE = re.compile(
    r"src/assignment/(?:prompts\.py|agent/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py)\Z"
)
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"
)


def restore_starter(stage, ref):
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", ref, "src/assignment"], cwd=ROOT
    )
    for name in paths.decode().splitlines():
        dest = stage / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(
            subprocess.check_output(["git", "show", f"{ref}:{name}"], cwd=ROOT)
        )


def stage_submission(source, stage):
    """Ignore unsafe/unrelated entries while retaining independently gradeable inputs."""
    issues = []
    seen = set()
    total = 0
    archive = source.is_file()

    def copy(name, size, read):
        nonlocal total
        is_ab = name.startswith("artifacts/") and AB_FILE.fullmatch(
            name.removeprefix("artifacts/")
        )
        if name not in REQUIRED and not CODE.fullmatch(name) and not is_ab:
            if archive:
                issues.append("unexpected entry: " + name)
            return
        path = Path(name)
        if CODE.fullmatch(name) and (
            "tests" in path.parts
            or path.name.startswith("test_")
            or path.name.endswith("_test.py")
        ):
            issues.append("test source is not a submission file: " + name)
            return
        if name.casefold() in seen:
            issues.append("duplicate entry: " + name)
            return
        seen.add(name.casefold())
        if size > MAX_FILE or size + total > MAX_TOTAL:
            issues.append("size limit: " + name)
            return
        total += size
        try:
            data = read()
            value = data.decode("utf-8")
            if not value.strip():
                raise ValueError("empty")
            if SECRET.search(data):
                raise ValueError("possible credential")
            if name.endswith(".json"):
                parsed = json.loads(value)
                if not isinstance(parsed, dict):
                    raise ValueError("expected a JSON object")
                if name.endswith("-result.json"):
                    if type(parsed.get("game_over")) is not bool or not isinstance(
                        parsed.get("history"), list
                    ):
                        raise ValueError("invalid game result")
                elif not all(
                    isinstance(parsed.get(k), list)
                    for k in ("prompts", "responses", "compactions")
                ):
                    raise ValueError("invalid trajectory")
            if name.endswith(".py"):
                ast.parse(value)
            if name.endswith(".patch") and not re.search(r"^@@ ", value, re.M):
                raise ValueError("no unified diff hunks")
        except (
            ValueError,
            SyntaxError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
            RecursionError,
        ) as exc:
            issues.append(f"{name}: {type(exc).__name__}: {str(exc)[:100]}")
            # Stage malformed code too: a syntax error must not fall back to starter code.
            if not (name.endswith(".py") and isinstance(exc, SyntaxError)):
                return
        dest = stage / name
        if (
            archive
            and CODE.fullmatch(name)
            and dest.is_file()
            and dest.read_bytes() == data
        ):
            issues.append("unchanged starter file: " + name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    if archive:
        with zipfile.ZipFile(source) as z:
            for info in z.infolist():
                name = info.filename
                if info.is_dir() and (
                    name in {"src/", "src/assignment/", "artifacts/"}
                    or re.fullmatch(r"src/assignment/agent/(?:[A-Za-z_]\w*/)*", name)
                ):
                    continue
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG):
                    issues.append("non-regular entry: " + name)
                    continue
                copy(name, info.file_size, lambda: z.read(info))
    else:
        issues.append("a directory was supplied; ZIP packaging is unverified")
        paths = [source / name for name in REQUIRED | {"src/assignment/prompts.py"}]
        paths.extend((source / "artifacts").glob("part3-*legal-moves-*.json"))
        paths.extend((source / "src/assignment/agent").rglob("*.py"))
        for path in paths:
            if (
                path.is_file()
                and path.resolve().is_relative_to(source)
                and not path.is_symlink()
            ):
                copy(
                    path.relative_to(source).as_posix(),
                    path.stat().st_size,
                    path.read_bytes,
                )
    issues.extend(
        "missing: " + name for name in sorted(REQUIRED) if not (stage / name).is_file()
    )
    if not any(name.startswith("src/assignment/agent/") for name in seen):
        issues.append("no agent source submitted")
    tags = model_tags(stage)
    if len(tags) != 2:
        issues.append(
            f"expected four conditions for two model tags; found {sorted(tags)}"
        )
    for tag in tags:
        for legal in (False, True):
            for suffix in (".json", "-result.json"):
                name = ab_name(tag, legal) + suffix
                if not (stage / "artifacts" / name).is_file():
                    issues.append("missing: artifacts/" + name)
    return issues


def model_tags(stage):
    return {
        m["tag"]
        for p in (stage / "artifacts").glob("*.json")
        if (m := AB_FILE.fullmatch(p.name))
    }
