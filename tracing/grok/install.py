"""Stage and install an additive native Grok tracing hook file."""

import argparse
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

MARKER = "coding-harness-tracing:grok:v1"


def get_hooks_file():
    return Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))) / "hooks" / "arize-tracing.json"


def stage_hooks(path, launcher):
    command = shlex.quote(str(launcher)) + " hook grok"
    events = {
        name: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
        for name in ["Stop", "StopFailure", "StopCancelled", "SessionEnd"]
    }
    events["Notification"] = [
        {"matcher": "idle_prompt", "hooks": [{"type": "command", "command": command, "timeout": 10}]}
    ]
    Path(path).write_text(json.dumps({"_owner": MARKER, "hooks": events}, indent=2) + "\n")


def install_hooks(stage, target):
    stage, target = Path(stage), Path(target)
    value = json.loads(stage.read_text())
    if value.get("_owner") != MARKER:
        raise ValueError("stage is not exporter-owned")
    if target.is_symlink() or (target.exists() and json.loads(target.read_text()).get("_owner") != MARKER):
        raise ValueError("target is not exporter-owned")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp." + str(os.getpid()))
    try:
        temporary.write_text(stage.read_text())
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {"install", "uninstall"}:
        from core.config import load_config
        from core.setup import (
            VENV_DIR,
            dry_run,
            info,
            prompt_backend,
            prompt_project_name,
            remove_harness_entry,
            write_config,
        )

        target = get_hooks_file()
        if sys.argv[1] == "uninstall":
            if target.exists() and not target.is_symlink() and json.loads(target.read_text()).get("_owner") == MARKER:
                if not dry_run():
                    target.unlink()
            remove_harness_entry("grok")
            return
        config = load_config()
        existing = config.get("harnesses", {}).get("grok")
        if not isinstance(existing, dict) or not existing.get("target"):
            backend, credentials = prompt_backend(config.get("harnesses"))
            project = prompt_project_name("grok", backend, config)
            if not dry_run():
                write_config(backend, credentials, "grok", project)
        with tempfile.TemporaryDirectory(prefix="grok-hook-stage-") as directory:
            stage = Path(directory) / "arize-tracing.json"
            stage_hooks(stage, VENV_DIR / "bin" / "arize-harness")
            if not dry_run():
                install_hooks(stage, target)
        info("Native Grok tracing hooks configured")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--install-to", type=Path)
    args = parser.parse_args()
    if args.install_to:
        install_hooks(args.stage, args.install_to)
    elif args.launcher:
        stage_hooks(args.stage, args.launcher)
    else:
        parser.error("--launcher or --install-to is required")


if __name__ == "__main__":
    main()
