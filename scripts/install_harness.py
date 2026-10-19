"""Install the locked DeepSeek Harness dependency tree inside this repository."""

from pathlib import Path
import shutil
import subprocess
from cad_agent.config import ROOT


def main():
    target = ROOT / ".harness"
    target.mkdir(exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(ROOT / "harness" / name, target / name)
    subprocess.run(
        [
            "npm",
            "ci",
            "--prefix",
            str(target),
            "--cache",
            str(ROOT / "artifacts/npm-cache"),
            "--registry=https://registry.npmjs.org",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
        check=True,
        cwd=ROOT,
    )


if __name__ == "__main__":
    main()
