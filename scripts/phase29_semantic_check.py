"""Phase 29 gate: real MiniLM + embedded Qdrant retrieval after five turns."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from memory.semantic import SemanticMemory
from settings import SemanticSettings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the test index")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="compa-semantic-"))
    settings = SemanticSettings(
        enabled=True,
        storage_path=root,
        collection="phase29_check",
        min_score=0.25,
        top_k=4,
        ingest_min_chars=1,
    )
    memory = SemanticMemory(settings)
    marker = "The Phase 29 release command is `pnpm build:desktop --phase29`."
    try:
        if not memory.warm_up():
            print("FAIL: embedding/Qdrant warmup failed")
            return 1
        memory.ingest(marker, category="code", source="verification")

        # Five unrelated turns must not surface the stored memory.
        for index in range(5):
            hits = memory.search(f"unrelated conversational turn number {index}")
            if any("pnpm build:desktop --phase29" in hit["text"] for hit in hits):
                print("FAIL: unrelated query retrieved the target memory")
                return 1

        hits = memory.search("Which command releases the Phase 29 build?")
        context = memory.context_for("Which command releases the Phase 29 build?")
        if not hits or "pnpm build:desktop --phase29" not in hits[0]["text"]:
            print("FAIL: related query did not retrieve the stored memory")
            return 1
        if "<memory_context>" not in context:
            print("FAIL: prompt block was not produced")
            return 1

        status = memory.status()
        print("PASS: semantic retrieval")
        print(f"  embedder: {status['embedder']} ({status['dimension']} dims)")
        print(f"  points:   {status['points']}")
        print(f"  score:    {hits[0]['score']:.3f}")
        print(f"  context:  {context.splitlines()[2][:110]}")
        return 0
    finally:
        memory.shutdown()
        if args.keep:
            print(f"  index kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
