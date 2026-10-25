#!/usr/bin/env python3
"""
slothsort - a lazy, local folder tidier.

Point it at a messy folder (hello, Downloads). A local open-weight model
(Gemma 3 via Ollama by default) looks at each loose file - its name, a peek at
its text, or the picture itself for images - and suggests a folder and a
readable name. You review the plan, apply it, and can undo it in one command.

Nothing leaves your machine. Standard library only; needs a running Ollama.

    python slothsort.py plan  ~/Downloads     # look, don't touch
    python slothsort.py apply ~/Downloads     # do what the plan says
    python slothsort.py undo  ~/Downloads     # put everything back
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

__version__ = "0.1.0"

DEFAULT_MODEL = "gemma3:4b"
STATE_DIR = ".slothsort"

CATEGORIES = [
    "Documents",    # letters, reports, PDFs, manuals, forms
    "Finance",      # invoices, receipts, bank statements, tax
    "Screenshots",  # screen captures
    "Photos",       # camera photos and other pictures
    "Code",         # source files, notebooks, configs
    "Data",         # csv, json, spreadsheets, exports
    "Installers",   # exe, msi, dmg, apk, setup files
    "Archives",     # zip, rar, 7z, tar
    "Media",        # audio and video
    "Notes",        # short personal notes, to-dos, drafts
    "Misc",         # anything that fits nowhere else
]

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".py", ".js", ".ts",
            ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg",
            ".toml", ".sql", ".sh", ".bat", ".ps1", ".java", ".c", ".cpp", ".ipynb"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# Used when the model is unreachable or answers nonsense.
EXT_FALLBACK = {
    **{e: "Code" for e in (".py", ".js", ".ts", ".java", ".c", ".cpp", ".sh",
                           ".bat", ".ps1", ".ipynb", ".html", ".css", ".sql")},
    **{e: "Data" for e in (".csv", ".tsv", ".json", ".xlsx", ".xls", ".xml",
                           ".yaml", ".yml")},
    **{e: "Installers" for e in (".exe", ".msi", ".dmg", ".pkg", ".apk", ".deb")},
    **{e: "Archives" for e in (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz")},
    **{e: "Media" for e in (".mp3", ".wav", ".m4a", ".flac", ".mp4", ".mkv",
                            ".mov", ".avi", ".webm")},
    **{e: "Photos" for e in IMAGE_EXT},
    **{e: "Documents" for e in (".pdf", ".doc", ".docx", ".odt", ".rtf",
                                ".ppt", ".pptx")},
    **{e: "Notes" for e in (".txt", ".md")},
}

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "new_name": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["category", "new_name", "reason"],
}

SYSTEM_PROMPT = f"""You tidy a messy downloads folder. For ONE file you get its
current name, type, size, date and, when available, a peek at its contents or
the image itself. Reply with JSON only:

- category: exactly one of {", ".join(CATEGORIES)}.
- new_name: a short, descriptive file name WITHOUT the extension, lowercase
  words joined by hyphens, at most 6 words. Describe what the file IS (e.g.
  "electricity-bill-september-2026", "resume-harsh-chaturvedi",
  "flight-booking-delhi-jaipur"). If the current name is already clear, keep
  its meaning. Never invent facts that are not in the name or contents.
- reason: one short sentence saying what you saw that decided it.

Screen captures (UI, windows, browser pages, chat apps) are Screenshots, not
Photos. Invoices, receipts, statements and payslips are Finance."""


# --------------------------------------------------------------------------
# Talking to the model
# --------------------------------------------------------------------------

class ModelError(RuntimeError):
    pass


class Ollama:
    """Tiny client for Ollama's /api/chat. No third-party packages."""

    def __init__(self, host: str, model: str, timeout: float = 300):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise ModelError(f"Ollama said {e.code}: {e.read().decode(errors='replace')[:200]}")
        except (urllib.error.URLError, OSError) as e:
            raise ModelError(f"can't reach Ollama at {self.host} ({e})")

    def check(self) -> None:
        req = urllib.request.Request(self.host + "/api/tags")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                tags = json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError) as e:
            raise ModelError(f"can't reach Ollama at {self.host}. Is it running? ({e})")
        names = {m.get("name") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise ModelError(f"model '{self.model}' isn't pulled. Run: ollama pull {self.model}")

    def classify(self, prompt: str, image_b64: str | None = None) -> dict:
        user = {"role": "user", "content": prompt}
        if image_b64:
            user["images"] = [image_b64]
        out = self._post("/api/chat", {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0},
            "keep_alive": "10m",
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, user],
        })
        content = out.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ModelError(f"model returned non-JSON: {content[:120]!r}")


# --------------------------------------------------------------------------
# Looking at files
# --------------------------------------------------------------------------

@dataclass
class Move:
    src: str            # path relative to the folder
    dst: str            # path relative to the folder
    category: str
    reason: str
    by: str             # "model" | "fallback" | "cache"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def peek_text(path: Path, limit: int = 1500) -> str | None:
    if path.suffix.lower() not in TEXT_EXT:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def peek_pdf(path: Path, limit: int = 1500) -> str | None:
    """Very rough: pull readable strings from the first chunk of a PDF.
    Good enough for many generated invoices/statements; silent otherwise."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        raw = path.read_bytes()[:400_000]
    except OSError:
        return None
    bits = re.findall(rb"\(([^()\\]{3,200})\)\s*Tj", raw)
    text = " ".join(b.decode("latin-1", "replace") for b in bits)
    return text[:limit] or None


def peek_zip(path: Path, limit: int = 40) -> str | None:
    """List the names inside a .zip so the model can name it by what it holds."""
    if path.suffix.lower() != ".zip":
        return None
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return None
    if not names:
        return None
    more = f"\n... and {len(names) - limit} more" if len(names) > limit else ""
    return "Files inside the archive:\n" + "\n".join(names[:limit]) + more


def image_b64(path: Path, max_bytes: int = 6_000_000) -> str | None:
    if path.suffix.lower() not in IMAGE_EXT:
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        return base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return None


def describe(path: Path) -> tuple[str, str | None]:
    st = path.stat()
    when = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
    lines = [
        f"File name: {path.name}",
        f"Extension: {path.suffix.lower() or '(none)'}",
        f"Size: {human_size(st.st_size)}",
        f"Last modified: {when}",
    ]
    peek = peek_text(path) or peek_pdf(path) or peek_zip(path)
    if peek:
        lines.append("First part of the contents:\n---\n" + peek + "\n---")
    img = image_b64(path)
    if img:
        lines.append("The image is attached.")
    if not peek and not img:
        lines.append("(Contents not readable; judge from the name and type.)")
    return "\n".join(lines), img


def fingerprint(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha1(f"{path.name}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()


_slug_bad = re.compile(r"[^a-z0-9]+")


def clean_name(raw: str, fallback: str) -> str:
    s = _slug_bad.sub("-", (raw or "").lower()).strip("-")
    s = "-".join(s.split("-")[:8])[:80].strip("-")
    if not s:
        s = _slug_bad.sub("-", fallback.lower()).strip("-")[:80] or "file"
    return s


def is_candidate(p: Path) -> bool:
    if not p.is_file() or p.is_symlink():
        return False
    name = p.name
    if name.startswith(".") or name.lower() in {"desktop.ini", "thumbs.db"}:
        return False
    if name.endswith((".crdownload", ".part", ".tmp", ".partial")):
        return False  # still downloading
    return True


# --------------------------------------------------------------------------
# Planning, applying, undoing
# --------------------------------------------------------------------------

class State:
    def __init__(self, root: Path):
        self.dir = root / STATE_DIR
        self.cache_file = self.dir / "cache.json"
        self.plan_file = self.dir / "plan.json"
        self.history_file = self.dir / "history.jsonl"

    def ensure(self) -> None:
        self.dir.mkdir(exist_ok=True)

    def load_cache(self) -> dict:
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_cache(self, cache: dict) -> None:
        self.ensure()
        self.cache_file.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def make_plan(root: Path, model: Ollama | None, *, limit: int | None = None,
              log=print) -> list[Move]:
    state = State(root)
    cache = state.load_cache()
    files = sorted((p for p in root.iterdir() if is_candidate(p)),
                   key=lambda p: p.name.lower())
    if limit:
        files = files[:limit]

    moves: list[Move] = []
    # Names already sitting in the category folders, as "Category/name".
    taken: set[str] = set()
    for cat in CATEGORIES:
        d = root / cat
        if d.is_dir():
            taken |= {f"{cat}/{p.name}".lower() for p in d.iterdir()}
    for i, path in enumerate(files, 1):
        fp = fingerprint(path)
        ext = path.suffix.lower()
        by = "model"
        decision = cache.get(fp)
        if decision:
            by = "cache"
        else:
            decision = None
            if model is not None:
                prompt, img = describe(path)
                t0 = time.time()
                try:
                    decision = model.classify(prompt, img)
                    log(f"  [{i}/{len(files)}] {path.name}  ({time.time() - t0:.1f}s)")
                except ModelError as e:
                    log(f"  [{i}/{len(files)}] {path.name}  model failed: {e}")
            if not decision or decision.get("category") not in CATEGORIES:
                decision = {"category": EXT_FALLBACK.get(ext, "Misc"),
                            "new_name": path.stem,
                            "reason": "guessed from the file extension"}
                by = "fallback"
            else:
                cache[fp] = decision

        stem = clean_name(decision.get("new_name", ""), path.stem)
        cat = decision["category"]
        dst_name = stem + ext
        n = 2
        while (f"{cat}/{dst_name}".lower() in taken):
            dst_name = f"{stem}-{n}{ext}"
            n += 1
        taken.add(f"{cat}/{dst_name}".lower())
        moves.append(Move(src=path.name, dst=f"{cat}/{dst_name}", category=cat,
                          reason=str(decision.get("reason", ""))[:200], by=by))

    state.save_cache(cache)
    state.plan_file.write_text(
        json.dumps({"created": _dt.datetime.now().isoformat(timespec="seconds"),
                    "moves": [asdict(m) for m in moves]}, indent=1),
        encoding="utf-8")
    return moves


def apply_plan(root: Path, log=print) -> int:
    state = State(root)
    try:
        plan = json.loads(state.plan_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SystemExit("No plan yet. Run `slothsort plan <folder>` first.")

    done = []
    for m in plan["moves"]:
        src, dst = root / m["src"], root / m["dst"]
        if not src.exists():
            log(f"  skip (gone): {m['src']}")
            continue
        if dst.exists():
            log(f"  skip (target exists): {m['dst']}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        done.append({"src": m["src"], "dst": m["dst"]})

    if done:
        batch = {"at": _dt.datetime.now().isoformat(timespec="seconds"), "moves": done}
        with open(state.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(batch) + "\n")
    state.plan_file.unlink(missing_ok=True)
    return len(done)


def undo_last(root: Path, log=print) -> int:
    state = State(root)
    try:
        lines = state.history_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        raise SystemExit("Nothing to undo.")
    batch = json.loads(lines[-1])
    restored = 0
    for m in reversed(batch["moves"]):
        src, dst = root / m["src"], root / m["dst"]
        if dst.exists() and not src.exists():
            shutil.move(str(dst), str(src))
            restored += 1
            try:
                dst.parent.rmdir()  # only removes it if now empty
            except OSError:
                pass
        else:
            log(f"  can't restore {m['dst']} (moved or replaced since)")
    state.history_file.write_text("".join(ln + "\n" for ln in lines[:-1]),
                                  encoding="utf-8")
    return restored


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def print_plan(moves: list[Move]) -> None:
    if not moves:
        print("Nothing to tidy. The folder is already sloth-approved.")
        return
    by_cat: dict[str, list[Move]] = {}
    for m in moves:
        by_cat.setdefault(m.category, []).append(m)
    for cat in CATEGORIES:
        if cat not in by_cat:
            continue
        print(f"\n{cat}/")
        for m in by_cat[cat]:
            new = m.dst.split("/", 1)[1]
            flag = "" if m.by != "fallback" else "  (guess)"
            print(f"  {m.src}\n    -> {new}{flag}\n       {m.reason}")
    print(f"\n{len(moves)} files planned. Nothing has moved yet.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="slothsort",
        description="Lazy, local folder tidier powered by an open-weight model.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("plan", "suggest moves and renames (touches nothing)"),
                           ("apply", "carry out the last plan"),
                           ("undo", "reverse the last apply"),
                           ("tidy", "plan and apply in one go (for schedulers)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("folder", type=Path)
        if name in ("plan", "tidy"):
            sp.add_argument("--model", default=os.environ.get("SLOTHSORT_MODEL", DEFAULT_MODEL))
            sp.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
            sp.add_argument("--limit", type=int, help="only look at the first N files")
            sp.add_argument("--offline-rules", action="store_true",
                            help="skip the model, sort by extension only")
    args = ap.parse_args(argv)

    root = args.folder.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2

    if args.cmd in ("plan", "tidy"):
        host = args.host if args.host.startswith("http") else "http://" + args.host
        model = None
        if not args.offline_rules:
            model = Ollama(host, args.model)
            try:
                model.check()
            except ModelError as e:
                print(f"slothsort: {e}", file=sys.stderr)
                return 1
        State(root).ensure()
        print(f"Looking through {root} with {'extension rules' if model is None else args.model}...")
        t0 = time.time()
        moves = make_plan(root, model, limit=args.limit)
        print_plan(moves)
        print(f"Took {time.time() - t0:.0f}s.")
        if args.cmd == "plan":
            if moves:
                print(f"Happy with it? Run: slothsort apply \"{args.folder}\"")
            return 0

    if args.cmd in ("apply", "tidy"):
        n = apply_plan(root)
        print(f"Moved {n} files. Changed your mind? Run: slothsort undo \"{args.folder}\"")
        return 0

    if args.cmd == "undo":
        n = undo_last(root)
        print(f"Put {n} files back where they were.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
