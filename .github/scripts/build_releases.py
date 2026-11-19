"""Build a download for every add-on whose current version has no GitHub Release yet.

Writes dist/<tag>/ with the file(s) and prints one line per add-on to release:
<tag>\t<title>
"""
import ast
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
ADDONS = ROOT / "addons"
DIST = ROOT / "dist"


def bl_info_version(py):
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "bl_info":
            info = ast.literal_eval(node.value)
            return ".".join(str(x) for x in info["version"])
    return None


def manifest_version(toml):
    m = re.search(r'^version\s*=\s*"([^"]+)"', toml.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def zip_dir(src, dest, with_folder):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_dir() or "__pycache__" in f.parts:
                continue
            arc = f.relative_to(src.parent if with_folder else src)
            z.write(f, arc.as_posix())


def existing_tags():
    out = subprocess.run(["gh", "release", "list", "--limit", "500", "--json", "tagName",
                          "--jq", ".[].tagName"], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def main():
    have = existing_tags()
    for folder in sorted(p for p in ADDONS.iterdir() if p.is_dir()):
        pkgs = [d for d in folder.iterdir() if d.is_dir() and (d / "__init__.py").exists()]
        files = []
        if pkgs:
            pkg = pkgs[0]
            manifest = pkg / "blender_manifest.toml"
            version = manifest_version(manifest) if manifest.exists() else bl_info_version(pkg / "__init__.py")
        else:
            files = sorted(folder.glob("*.py"))
            version = bl_info_version(files[0]) if files else None
        if not version:
            print(f"skip {folder.name}: no version found", file=sys.stderr)
            continue
        tag = f"{folder.name}-v{version}"
        if tag in have:
            continue
        out = DIST / tag
        out.mkdir(parents=True, exist_ok=True)
        if pkgs:
            # extensions keep the manifest at the root of the zip
            zip_dir(pkg, out / f"{pkg.name}_{version}.zip", with_folder=not manifest.exists())
        else:
            for f in files:
                shutil.copy2(f, out / f.name)
        title = (folder / "README.md").read_text(encoding="utf-8").splitlines()[0].lstrip("# ").strip()
        print(f"{tag}\t{title} {version}")


if __name__ == "__main__":
    main()
