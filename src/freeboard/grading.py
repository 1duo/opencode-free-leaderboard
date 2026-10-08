from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .config import Settings


class GradingUnavailable(RuntimeError):
    pass


def gpqa_score(answer: str, text: str) -> float:
    matches = re.findall(r"(?:^|\n)FINAL:\s*([ABCD])\s*[.!]?\s*$", text.strip())
    return float(len(matches) == 1 and matches[0] == answer)


class DockerGrader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tag = "opencode-free-grader:20261008"

    def available(self) -> bool:
        return bool(shutil.which("docker"))

    def build(self, manifest: dict, checkout: Path) -> str:
        if not self.available():
            raise GradingUnavailable("Docker is not installed; code grading remains pending")
        if manifest.get("grader_image"):
            result = subprocess.run(["docker", "image", "inspect", manifest['grader_image'], "--format", "{{.Id}}"],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and result.stdout.strip() == manifest['grader_image']:
                return manifest['grader_image']
            raise GradingUnavailable("This season's immutable grader image is missing; restore it before regrading")
        with tempfile.TemporaryDirectory(dir=self.settings.state) as folder:
            context = Path(folder)
            shutil.copy(checkout / "grading/Dockerfile", context)
            shutil.copy(checkout / "grading/worker.py", context)
            shutil.copy(checkout / "grading/requirements.txt", context)
            for name, path in manifest["upstream_paths"].items():
                shutil.copytree(Path(path), context / name)
            result = subprocess.run(["docker", "build", "--tag", self.tag, str(context)],
                                    capture_output=True, text=True, timeout=600)
            if result.returncode:
                raise GradingUnavailable("Docker grading image build failed; see local Docker logs")
        return self.image_id()

    def image_id(self) -> str:
        try:
            result = subprocess.run(["docker", "image", "inspect", self.tag, "--format", "{{.Id}}"],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            raise GradingUnavailable("Docker grading image unavailable") from None
        if result.returncode or not result.stdout.startswith("sha256:"):
            raise GradingUnavailable("Build the pinned grading image first")
        return result.stdout.strip()

    def grade(self, item: dict, text: str, image: str) -> float:
        name = "freeboard-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--name", name, "--interactive",
                   "--network=none", "--read-only", "--user=65534:65534",
                   "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--cpus=1", "--memory=1g", "--memory-swap=1g", "--pids-limit=64",
                   "--tmpfs=/tmp:rw,noexec,nosuid,size=256m", "--tmpfs=/dev/shm:rw,noexec,nosuid,size=64m",
                   image]
        try:
            result = subprocess.run(command, input=json.dumps({"item": item, "text": text}),
                                    capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=15)
            # A whole-container timeout is infrastructure uncertainty, not a failed answer.
            raise GradingUnavailable("Container exceeded outer timeout; grading pending") from None
        except OSError:
            raise GradingUnavailable("Docker unavailable") from None
        if result.returncode:
            raise GradingUnavailable("Grading container failed")
        try:
            report = json.loads(result.stdout.splitlines()[-1])
            score = float(report["score"])
            if not 0 <= score <= 1:
                raise ValueError()
            return score
        except (ValueError, KeyError, IndexError):
            raise GradingUnavailable("Invalid grading report") from None

    def validate(self, image: str) -> None:
        for item, good, bad in [
            ({"benchmark": "livebench", "stratum": "spatial", "answer": "2"}, "**2**", "**3**"),
            ({"benchmark": "livebench", "stratum": "zebra_puzzle", "release": "2024-11-25", "answer": "1, red"},
             "<solution>1, red</solution>", "<solution>2, blue</solution>"),
            ({"benchmark": "synthetic_code", "tests": [["2\n", "4\n"]]},
             "```python\nprint(int(input())*2)\n```", "```python\nprint(0)\n```"),
        ]:
            if self.grade(item, good, image) != 1 or self.grade(item, bad, image) != 0:
                raise GradingUnavailable("Pinned grader fixtures failed")
