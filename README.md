# 🦥 slothsort

**A lazy, local folder tidier.** Point it at your messy Downloads folder. An open-weight model running on your own machine (Gemma 3 4B via [Ollama](https://ollama.com)) looks at every loose file. It reads text files and PDFs, and actually *looks* at images. Then it suggests a folder and a readable name for each one.

You review the plan, apply it with one command, and undo it with another. Nothing leaves your computer.

```
download.pdf                      -> Finance/payslip-a-sharma-september-2026.pdf
Screenshot 2026-10-23 104512.png  -> Code/android-gradle-build-failure.png
New Text Document.txt             -> Notes/groceries-sunday-plumber.txt
```
*(Real output from Gemma 3 4B on a CPU-only Windows laptop. The full log is in [`demo/run-log.txt`](demo/run-log.txt).)*

Built for the [DEV Hacktoberfest Open-Source AI Challenge, Week 3: Slow and Steady](https://dev.to/challenges/hacktoberfest-week3-2026-10-19).

## Why it's built like this

- **Local model, local files.** Downloads folders are full of payslips, bank exports, rental agreements and boarding passes. Sending their contents to a cloud API just to choose a folder name is a bad trade. Here, the only network call goes to `localhost:11434`.
- **Plan first, then apply.** `plan` changes nothing. It writes `.slothsort/plan.json` and prints what it *would* do.
- **Always undoable.** Every `apply` is logged in `.slothsort/history.jsonl`, and `undo` reverses the last one.
- **Never overwrites.** Name clashes get `-2`, `-3`… If a target appears between planning and applying, that move is skipped.
- **Cheap on re-runs.** Each decision is cached by file fingerprint (name, size, mtime), so a nightly run only spends model time on new files.
- **Falls back gracefully.** If the model is down or returns junk, the file is sorted by extension and marked `(guess)`.
- **Guard rails on model output.** Ollama's structured outputs constrain the answer to a JSON schema with a fixed category list. Names are then slugified, so the model can't write `../../` paths.
- **Zero dependencies.** Python 3.9+ standard library only.

## Quick start

```bash
# 1. Install Ollama (https://ollama.com) and pull a model
ollama pull gemma3:4b

# 2. Look, don't touch
python slothsort.py plan ~/Downloads

# 3. Like it? Do it.
python slothsort.py apply ~/Downloads

# 4. Changed your mind?
python slothsort.py undo ~/Downloads
```

Options:

| Flag / env | Default | What it does |
|---|---|---|
| `--model` / `SLOTHSORT_MODEL` | `gemma3:4b` | Any Ollama model. Vision models (Gemma 3 4B+, Qwen2.5-VL, LLaVA) also read images. |
| `--host` / `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama lives |
| `--limit N` | – | Only look at the first N files (handy for a first try) |
| `--offline-rules` | off | Skip the model, sort by extension only |

`slothsort tidy <folder>` plans and applies in one go. It's meant for a scheduler, if you want maximum sloth:

```bat
:: Windows: tidy Downloads every night at 2am
schtasks /create /sc daily /st 02:00 /tn slothsort /tr "python C:\path\to\slothsort.py tidy %USERPROFILE%\Downloads"
```
```bash
# macOS / Linux crontab
0 2 * * * python3 /path/to/slothsort.py tidy ~/Downloads
```

## Categories

`Documents`, `Finance`, `Screenshots`, `Photos`, `Code`, `Data`, `Installers`, `Archives`, `Media`, `Notes`, `Misc`. Edit `CATEGORIES` at the top of `slothsort.py` to make them yours. The schema and prompt update automatically.

## What the model sees

For each top-level file (sub-folders are left alone):

- name, extension, size, modified date
- the first ~1,500 characters of text-like files (`.txt .md .csv .json .py` …)
- readable text strings from simple PDFs
- the image itself, for `.png .jpg .webp` … under 6 MB

Hidden files, `desktop.ini`, and half-finished downloads (`.crdownload`, `.part`) are skipped.

## Demo

`demo/messy-downloads/` is a made-up cluttered Downloads folder. Every name, company and amount in it is fictional. Run it yourself:

```bash
cp -r demo/messy-downloads /tmp/try && python slothsort.py plan /tmp/try
```

On Windows, double-click `run-demo.bat`. It copies the demo folder, runs plan → apply → undo, and saves everything to `demo/run-log.txt`.

## Tests

```bash
python -m unittest -v
```

The tests run against a fake Ollama server, so they need no model or GPU.

## License

MIT
