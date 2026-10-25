"""Tests run against a fake Ollama server, so no model is needed.

    python -m unittest -v
"""
import io
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import slothsort as ss


class FakeOllama(BaseHTTPRequestHandler):
    answers: dict = {}       # file name -> dict or raw string
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "gemma3:4b"}]})
        else:
            self._send({}, 404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = req["messages"][-1]
        name = user["content"].split("\n")[0].removeprefix("File name: ")
        FakeOllama.seen.append({"name": name, "has_image": bool(user.get("images")),
                                "format": req.get("format")})
        ans = FakeOllama.answers.get(name, {"category": "Misc", "new_name": name,
                                            "reason": "default"})
        content = ans if isinstance(ans, str) else json.dumps(ans)
        self._send({"message": {"role": "assistant", "content": content}})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        FakeOllama.answers = {}
        FakeOllama.seen = []
        self.model = ss.Ollama(self.host, "gemma3:4b")

    def tearDown(self):
        shutil.rmtree(self.root)

    def touch(self, name, data=b"x"):
        p = self.root / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        return p

    def plan(self, model="default"):
        return ss.make_plan(self.root, self.model if model == "default" else model,
                            log=lambda *a: None)


class TestPlan(Base):
    def test_model_decides_folder_and_name(self):
        self.touch("Invoice_3321 (1).txt", "Invoice no 3321\nAirtel broadband\nTotal Rs 799")
        FakeOllama.answers["Invoice_3321 (1).txt"] = {
            "category": "Finance", "new_name": "Airtel Broadband Invoice 3321",
            "reason": "it is an invoice"}
        [m] = self.plan()
        self.assertEqual(m.dst, "Finance/airtel-broadband-invoice-3321.txt")
        self.assertEqual(m.by, "model")
        self.assertTrue((self.root / "Invoice_3321 (1).txt").exists(), "plan must not move")

    def test_text_is_peeked_and_images_are_attached(self):
        self.touch("notes.txt", "buy milk")
        self.touch("Screenshot 2026-10-21.png", b"\x89PNG fake")
        self.plan()
        seen = {s["name"]: s for s in FakeOllama.seen}
        self.assertFalse(seen["notes.txt"]["has_image"])
        self.assertTrue(seen["Screenshot 2026-10-21.png"]["has_image"])
        self.assertEqual(seen["notes.txt"]["format"]["properties"]["category"]["enum"],
                         ss.CATEGORIES)

    def test_zip_contents_are_listed(self):
        import zipfile
        with zipfile.ZipFile(self.root / "files.zip", "w") as z:
            z.writestr("assignment3/main.c", "int main(){}")
        prompt, img = ss.describe(self.root / "files.zip")
        self.assertIn("assignment3/main.c", prompt)
        self.assertIsNone(img)

    def test_bad_model_output_falls_back_to_extension(self):
        self.touch("setup.exe")
        self.touch("song.mp3")
        FakeOllama.answers["setup.exe"] = "I think this is an installer!"   # not JSON
        FakeOllama.answers["song.mp3"] = {"category": "Music!!", "new_name": "x", "reason": ""}
        moves = {m.src: m for m in self.plan()}
        self.assertEqual(moves["setup.exe"].dst, "Installers/setup.exe")
        self.assertEqual(moves["song.mp3"].dst, "Media/song.mp3")
        self.assertEqual(moves["song.mp3"].by, "fallback")

    def test_unreachable_model_still_plans(self):
        self.touch("report.pdf")
        dead = ss.Ollama("http://127.0.0.1:9", "gemma3:4b", timeout=1)
        [m] = self.plan(model=dead)
        self.assertEqual(m.dst, "Documents/report.pdf")

    def test_name_collisions_get_numbered(self):
        (self.root / "Finance").mkdir()
        (self.root / "Finance" / "bill.pdf").write_text("old")
        for n in ("a.pdf", "b.pdf"):
            self.touch(n)
            FakeOllama.answers[n] = {"category": "Finance", "new_name": "bill", "reason": "r"}
        dsts = sorted(m.dst for m in self.plan())
        self.assertEqual(dsts, ["Finance/bill-2.pdf", "Finance/bill-3.pdf"])

    def test_skips_hidden_partial_and_folders(self):
        self.touch(".hidden")
        self.touch("movie.mp4.crdownload")
        self.touch("desktop.ini")
        (self.root / "SomeFolder").mkdir()
        self.touch("keep.txt")
        self.assertEqual([m.src for m in self.plan()], ["keep.txt"])

    def test_cache_means_no_second_model_call(self):
        self.touch("a.txt")
        self.plan()
        self.assertEqual(len(FakeOllama.seen), 1)
        [m] = self.plan()
        self.assertEqual(len(FakeOllama.seen), 1)
        self.assertEqual(m.by, "cache")

    def test_dangerous_names_are_cleaned(self):
        self.touch("x.txt")
        FakeOllama.answers["x.txt"] = {"category": "Notes",
                                       "new_name": "../../etc/passwd  NOTES:*?", "reason": "r"}
        [m] = self.plan()
        self.assertEqual(m.dst, "Notes/etc-passwd-notes.txt")


class TestApplyUndo(Base):
    def test_apply_then_undo_round_trip(self):
        self.touch("Invoice.txt", "invoice")
        self.touch("IMG_0042.jpg", b"\xff\xd8 fake")
        FakeOllama.answers["Invoice.txt"] = {"category": "Finance", "new_name": "gas bill",
                                             "reason": "r"}
        FakeOllama.answers["IMG_0042.jpg"] = {"category": "Photos", "new_name": "sunset jaipur",
                                              "reason": "r"}
        self.plan()
        self.assertEqual(ss.apply_plan(self.root, log=lambda *a: None), 2)
        self.assertTrue((self.root / "Finance/gas-bill.txt").exists())
        self.assertTrue((self.root / "Photos/sunset-jaipur.jpg").exists())
        self.assertFalse((self.root / "Invoice.txt").exists())

        self.assertEqual(ss.undo_last(self.root, log=lambda *a: None), 2)
        self.assertTrue((self.root / "Invoice.txt").exists())
        self.assertTrue((self.root / "IMG_0042.jpg").exists())
        self.assertFalse((self.root / "Finance").exists(), "empty folder cleaned up")

    def test_apply_never_overwrites(self):
        self.touch("a.txt", "new")
        FakeOllama.answers["a.txt"] = {"category": "Notes", "new_name": "a", "reason": "r"}
        self.plan()
        (self.root / "Notes").mkdir()
        (self.root / "Notes/a.txt").write_text("precious")
        self.assertEqual(ss.apply_plan(self.root, log=lambda *a: None), 0)
        self.assertEqual((self.root / "Notes/a.txt").read_text(), "precious")
        self.assertEqual((self.root / "a.txt").read_text(), "new")

    def test_cli_plan_apply_undo(self):
        self.touch("todo.md", "- call mom")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ss.main(["plan", str(self.root), "--host", self.host]), 0)
            self.assertEqual(ss.main(["apply", str(self.root)]), 0)
            self.assertEqual(ss.main(["undo", str(self.root)]), 0)
        text = out.getvalue()
        self.assertIn("Nothing has moved yet", text)
        self.assertIn("Moved 1 files", text)
        self.assertIn("Put 1 files back", text)
        self.assertTrue((self.root / "todo.md").exists())

    def test_cli_reports_missing_model(self):
        with redirect_stdout(io.StringIO()):
            import contextlib
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = ss.main(["plan", str(self.root), "--host", self.host, "--model", "llama9"])
        self.assertEqual(rc, 1)
        self.assertIn("ollama pull llama9", err.getvalue())


if __name__ == "__main__":
    unittest.main()
