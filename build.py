#!/usr/bin/env python3
"""Create and inspect one self-contained private local plugin source archive."""
import argparse
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
args = parser.parse_args()
manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
package_name = manifest["name"]
assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", manifest["name"])
assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
assert len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30
assert (ROOT / "server/app.py").is_file()
assert (ROOT / "skills/research-course/SKILL.md").read_text(encoding="utf-8").startswith("---\nname: research-course\n")
output = args.output.resolve()
if ROOT == output.parent or ROOT in output.parents:
    raise SystemExit("Place the archive outside the plugin directory")
output.parent.mkdir(parents=True, exist_ok=True)
root_files = {".gitignore", "README.md", "TESTING.md", "START-HERE.txt", "plugin.json", "mcp.json", "build.py",
              "research.py", "workflow.py", "analyze.py", "setup-bridge.py", "show-mcp-config.py", "run-python.cmd",
              "start-bridge.cmd", "start-bridge.sh", "install-windows.cmd", "collect-materials.cmd", "collect-materials.sh",
              "personal-auto.cmd", "personal-auto.sh"}
root_files |= {"LICENSE", "THIRD_PARTY_NOTICES.md", "PUBLIC_README.md", "desktop_settings.py", "desktop_views.py", "desktop.py", "Run-Assistant.vbs", "extension-id.json", "personal-connection.json"}
source_folders = {"server", "extension", "skills", "tests", "packaging"}
files = []
for path in ROOT.rglob("*"):
    relative = path.relative_to(ROOT)
    if not path.is_file() or path.is_symlink() or any(part in {"__pycache__", ".venv", ".git", "output", "exports", "analysis", "for-ai"} for part in relative.parts):
        continue
    if path.parent == ROOT:
        if path.name not in root_files:
            continue
    elif relative.parts[0] not in source_folders or path.suffix not in {".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".png", ".iss", ".isl", ".manifest", ".ps1"}:
        continue
    if re.match(r"(?:client_secret|credentials|token|google-token|pairing)", path.name):
        continue
    if path.suffix == ".json":
        json.loads(path.read_text(encoding="utf-8"))
    files.append(path)
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, package_name + "/" + str(path.relative_to(ROOT)))
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
    names = archive.namelist()
    assert all(name.startswith(package_name + "/") and ".." not in Path(name).parts for name in names)
    for name in names:
        if name.endswith(".json"):
            json.loads(archive.read(name))
    for required in ("plugin.json", "mcp.json", "extension/manifest.json", "server/app.py", "server/bundle.py", "workflow.py", "collect-materials.cmd", "personal-auto.cmd", "START-HERE.txt", "skills/research-course/SKILL.md", "README.md"):
        assert package_name + "/" + required in names
print(json.dumps({"archive": str(output), "files": len(files), "bytes": output.stat().st_size, "inspected": True}, ensure_ascii=False))
