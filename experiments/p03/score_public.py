"""Compare public literary text independently of LibriVox announcements.

Input must contain full ASR text, never ASR segments/timestamps. The reference
is published Chekhov, not a manually verified transcript of this performance.
"""
import argparse
import json
from pathlib import Path

from probe import LOCAL, ROOT, compare, sha256

START = "Теперь слушайте"
END = "вернулся домой."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=LOCAL / "public-results.json")
    parser.add_argument("--output", type=Path, default=LOCAL / "public-scored.json")
    args = parser.parse_args()
    args.output.resolve().relative_to(LOCAL.resolve())
    reference_path = ROOT / "public-reference.txt"
    reference = reference_path.read_text(encoding="utf-8")
    report = json.loads(args.input.read_text(encoding="utf-8"))
    report["reference"] = {
        "source": "https://ilibrary.ru/text/438/p.1/index.html",
        "kind": "published literary text; not a verbatim verified audio transcript",
        "sha256": sha256(reference_path),
        "start": START, "end": END,
    }
    for row in report["runs"]:
        text = " ".join(row["result"]["text"].split())
        start = text.index(START)
        end = text.index(END, start) + len(END)
        row["literary_quality"] = compare(reference, text[start:end])
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Scored {len(report['runs'])} public runs; announcements excluded")


if __name__ == "__main__":
    main()
