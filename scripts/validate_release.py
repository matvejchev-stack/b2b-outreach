#!/usr/bin/env python3
"""Check release structure, local links and version consistency without network."""

import json
import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / "plugin.json").read_text())
    name, version = manifest["name"], manifest["version"]
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "invalid release version"
    claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert (claude["name"], claude["version"]) == (name, version), "manifest versions differ"
    skill = ROOT / "skills" / name / "SKILL.md"
    entry = skill.read_text()
    assert entry.startswith("---\n") and "\n---\n" in entry[4:], "missing frontmatter"
    frontmatter = entry.split("---\n", 2)[1]
    assert re.search(rf"^name: {re.escape(name)}$", frontmatter, re.M), "skill name differs"
    assert f'version: "{version}"' in frontmatter, "skill version differs"
    assert version in (ROOT / "README.md").read_text(), "README version missing"
    assert version in (ROOT / "CHANGELOG.md").read_text(), "changelog version missing"

    for path in [ROOT / ".agents/plugins/marketplace.json", ROOT / ".claude-plugin/marketplace.json"]:
        catalog = json.loads(path.read_text())
        assert catalog["name"] == "matveychev-b2b", "marketplace names differ"
        assert len(catalog["plugins"]) == 1
        plugin = catalog["plugins"][0]
        assert plugin["name"] == name
        source = plugin["source"]
        relative = source["path"] if isinstance(source, dict) else source
        assert relative == "./" and (ROOT / relative / "plugin.json").is_file(), "plugin path invalid"

    count = 0
    for path in ROOT.rglob("*.md"):
        if ".git" in path.parts:
            continue
        for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", path.read_text()):
            if ":" in target or target.startswith(("#", "/")):
                continue
            target = unquote(target.split("#", 1)[0])
            resolved = (path.parent / target).resolve()
            assert resolved.is_relative_to(ROOT) and resolved.exists(), f"broken local link: {path.name}: {target}"
            count += 1
    for path in ROOT.rglob("*.json"):
        if ".git" not in path.parts:
            json.loads(path.read_text())
    print(f"Release {version}: manifests, skill entrypoint, catalogs, JSON and {count} local links OK")


if __name__ == "__main__":
    main()
