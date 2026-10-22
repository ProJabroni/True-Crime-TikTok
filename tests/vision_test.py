#!/usr/bin/env python3
"""Quick check of the 'no words in pictures' step on sample pictures (run by editing vision-test.txt).

Writes state/vision-test.log and before/after pictures to state/vision-test/.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_video as mv  # noqa: E402

out_dir = ROOT / "state" / "vision-test"
out_dir.mkdir(parents=True, exist_ok=True)
lines = []


def note(*a):
    text = " ".join(str(x) for x in a)
    print(text, flush=True)
    lines.append(text)


mv.log = note
for src in sorted((ROOT / "tests").glob("*.jpg")):
    work = out_dir / src.name
    shutil.copy(src, work)
    ocr = []
    mv.text_in_image(work, ocr)
    vision = mv.vision_text_boxes(work)
    note(f"{src.name}: OCR boxes {ocr} | Gemini vision boxes {vision}")
    mv.remove_lettering(work, mv.lettering_boxes(work))
    note(f"{src.name}: after painting, OCR {mv.text_in_image(work)} | vision {mv.vision_text_boxes(work)}")
(ROOT / "state" / "vision-test.log").write_text("\n".join(lines) + "\n")
