"""Check template/grok-bot.json against the repository.

  python3 scripts/check_template.py           verify every claimed sha256
  python3 scripts/check_template.py --write   recompute and rewrite the hashes

Also checks that the release in the template, pins.env and the Start prompt in
README.md agree, so a release bump cannot leave one of them behind.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "template" / "grok-bot.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_release() -> str:
    for line in (ROOT / "pins.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("BOT_RELEASE="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("pins.env has no BOT_RELEASE")


def main() -> int:
    data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    release = pinned_release()
    errors: list[str] = []
    skills = sorted(p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md"))

    if "--write" in sys.argv:
        data["skills"] = {name: sha256(ROOT / "skills" / name / "SKILL.md") for name in skills}
        data["agent"] = {"path": "agents/pickletime-bot.md", "sha256": sha256(ROOT / "agents" / "pickletime-bot.md")}
        data["source"]["release"] = release.lstrip("v")
        TEMPLATE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {TEMPLATE.relative_to(ROOT)}: {len(skills)} skill(s), release {release}")
        return 0

    if set(data.get("skills", {})) != set(skills):
        errors.append(f"skills claimed {sorted(data.get('skills', {}))} != on disk {skills}")
    for name, digest in data.get("skills", {}).items():
        path = ROOT / "skills" / name / "SKILL.md"
        if not path.is_file():
            errors.append(f"{name}: {path} missing")
        elif sha256(path) != digest:
            errors.append(f"skills.{name} sha256 does not match {path.relative_to(ROOT)}")
    agent = data.get("agent", {})
    agent_path = ROOT / agent.get("path", "agents/pickletime-bot.md")
    if not agent_path.is_file() or sha256(agent_path) != agent.get("sha256"):
        errors.append("agent sha256 does not match agents/pickletime-bot.md")
    if data.get("source", {}).get("release") != release.lstrip("v"):
        errors.append(f"source.release {data.get('source', {}).get('release')} != pins.env {release}")
    for doc in ("README.md", "SETUP.md", "skills/pickletime-bot-bootstrap/SKILL.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for found in set(re.findall(r"(?:blob/|--branch )(v\d+\.\d+\.\d+)", text)):
            if found != release:
                errors.append(f"{doc} pins {found}, pins.env says {release}")
    for template_skill in data.get("templateSkillNames", []):
        if template_skill not in skills:
            errors.append(f"templateSkillNames: {template_skill} is not a skill")

    for err in errors:
        print(f"FAIL: {err}")
    if errors:
        return 1
    print(f"ok: template {data.get('status')}, release {release}, {len(skills)} verified skill(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
