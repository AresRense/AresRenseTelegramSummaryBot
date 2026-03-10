import argparse
import shutil
import zipfile
from pathlib import Path
import rarfile
import tarfile
import py7zr
import json

ARCHIVE_EXTENSIONS = [".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tar.bz2"]

default_src = ""
default_out = ""
default_ext = ""

CONFIG_FILE = Path(__file__).parent / "extract_files_config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"default_src": None, "default_out": None, "default_ext": None}


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def collect_from_archive(archive_path: Path, out_dir: Path, ext: str):
    suffix = "".join(archive_path.suffixes).lower()

    if suffix == ".zip":
        return collect_from_zip(archive_path, out_dir, ext)

    if suffix == ".rar":
        return collect_from_rar(archive_path, out_dir, ext)

    if suffix == ".7z":
        return collect_from_7z(archive_path, out_dir, ext)

    elif suffix in (".tar.bz2", ".tar.gz", ".tar"):
        return collect_from_tar(archive_path, out_dir, ext)

    return []


def collect_from_tar(tar_path: Path, out_dir: Path, ext: str) -> list[str]:
    collected = []
    try:
        with tarfile.open(tar_path, "r:*") as tf:
            members = [m for m in tf.getnames() if m.lower().endswith(ext)]
            for member in members:
                data = tf.extractfile(member).read()
                filename = Path(member).name
                dest = unique_dest(out_dir, filename)
                dest.write_bytes(data)
                collected.append(
                    f"[TAR] {tar_path.name}/{member} -> {dest.name}")
    except tarfile.TarError:
        print(f"skipped bad tar: {tar_path}")
    return collected


def collect_from_rar(rar_path: Path, out_dir: Path, ext: str) -> list[str]:
    collected = []
    try:
        with rarfile.RarFile(rar_path, "r") as rf:
            members = [m for m in rf.namelist() if m.lower().endswith(ext)]
            for member in members:
                data = rf.read(member)
                filename = Path(member).name
                dest = unique_dest(out_dir, filename)
                dest.write_bytes(data)
                collected.append(
                    f"[RAR] {rar_path.name}/{member} -> {dest.name}")
    except rarfile.BadRarFile:
        print(f"skipped bad rar: {rar_path}")
    return collected


def collect_from_7z(szp_path: Path, out_dir: Path, ext: str) -> list[str]:
    collected = []
    try:
        with py7zr.SevenZipFile(szp_path, "r") as zf:
            members = [m for m in zf.getnames() if m.lower().endswith(ext)]
            zf.extract(targets=members, path=out_dir)
            collected = [
                f"[7z] {szp_path.name}/{m} -> {Path(m).name}" for m in members]
    except py7zr.exceptions.Bad7zFile:
        print(f"skipped bad 7z: {szp_path}")
    return collected


def collect_from_zip(zip_path: Path, out_dir: Path, ext: str) -> list[str]:
    collected = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            md_members = [
                m for m in zf.namelist() if m.lower().endswith(ext)]
            for member in md_members:
                filename = Path(member).name
                dest = unique_dest(out_dir, filename)
                data = zf.read(member)
                dest.write_bytes(data)
                collected.append(
                    f"[ZIP] {zip_path.name}/{member} -> {dest.name}")
    except zipfile.BadZipFile:
        print("skipped bad zip")
    return collected


def unique_dest(out_dir: Path, filename: str) -> Path:
    dest = out_dir / filename
    if not dest.exists():
        return dest

    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    while True:
        dest = out_dir / f"{stem}_{counter}{suffix}"
        if not dest.exists():
            return dest
        counter += 1


def collect_from_folder(folder: Path, out_dir: Path, ext: str) -> list[str]:
    collected = []
    for md_file in folder.rglob(f"*{ext}"):
        dest = unique_dest(out_dir, md_file.name)
        shutil.copy2(md_file, dest)
        collected.append(
            f"[DIR] {md_file.relative_to(folder.parent)} -> {dest.name}")
    return collected


def ask_for_path(prompt: str, default: str = None, must_exist: bool = False) -> Path:
    while True:
        if default:
            raw = input(f"{prompt} [{default}]: ").strip()
            if raw == "":
                raw = default
        else:
            raw = input(f"{prompt}: ").strip()
        path = Path(raw)

        if must_exist and not path.exists():
            print("no such file or directory")
        else:
            return path


def ask_for_ext(prompt: str, default: str = None) -> str:
    while True:
        if default:
            raw = input(f"{prompt} [{default}]: ").strip()
            if raw == "":
                return default
        else:
            raw = input(f"{prompt}: ").strip()
        raw = normalize_ext(raw)
        if len(raw) > 1:
            return raw
        print("invalid extension")


def normalize_ext(raw: str) -> str:
    raw = raw.strip().lower()
    if not raw.startswith("."):
        raw = "." + raw
    return raw


def main():

    parser = argparse.ArgumentParser(
        description="Collect all files into one folder")
    parser.add_argument("--src", default=None, help="Where to search")
    parser.add_argument("--out", default=None, help="Where to store")
    parser.add_argument("--ext", default=None, help="What to searh")
    parser.add_argument("--set-defaults", action="store_true",
                        help="Save current extension as default")
    args = parser.parse_args()

    config = load_config()

    if args.src is None:
        src = ask_for_path("Where to scan: ",
                           default=config["default_src"], must_exist=True)
    else:
        src = Path(args.src)

        if not src.exists():
            print(f"folder not found {src}")
            return

    if args.out is None:
        out = ask_for_path("Output folder for .__ files: ",
                           default=config["default_out"])
    else:
        out = Path(args.out)

    if args.ext is None:
        ext = ask_for_ext("File extension to search for: ",
                          default=config["default_ext"])
    else:
        ext = normalize_ext(args.ext)

    if args.set_defaults or not all([config["default_src"], config["default_out"], config["default_ext"]]):
        save = input(
            "\n Save these config files as defaults? (y/n): ").strip().lower()
        if save == "y":
            config["default_src"] = str(src)
            config["default_out"] = str(out)
            config["default_ext"] = ext
            save_config(config)
            print("defaults saved\n")

    out.mkdir(parents=True, exist_ok=True)

    print(f"Search : {src}")
    print(f"Store : {out}\n")

    log: list[str] = []

    for archive in src.rglob("*"):
        if "".join(archive.suffixes).lower() in ARCHIVE_EXTENSIONS:
            print(f" ARCHIVE -> {archive.relative_to(src)}")
            log += collect_from_archive(archive, out, ext)

    for md_file in src.rglob(f"*{ext}"):
        if not any("".join(p.suffixes).lower() in ARCHIVE_EXTENSIONS for p in md_file.parents):
            print(f" MD -> {md_file.relative_to(src)}")

            dest = unique_dest(out, md_file.name)
            shutil.copy2(md_file, dest)
            log.append(f" [DIR] {md_file.relative_to(src)} -> {dest.name}")

    print("\n Results:")
    if log:
        for line in log:
            print(line)
        print(f"\n {len(log)} *{ext} moved to:\n {out}")
    else:
        print("None was found")


if __name__ == "__main__":
    main()
