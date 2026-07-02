from __future__ import annotations

import argparse
import json

from ollama_client import ollama_health
from vault import vault_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Show ChatIFU local vault status.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--deep", action="store_true", help="Also check Ollama embedding health.")
    parser.add_argument("--exact-vectors", action="store_true", help="Use an exact vector count.")
    args = parser.parse_args()

    stats = vault_stats(exact_vectors=args.exact_vectors)
    if args.deep:
        stats["ollama"] = ollama_health()

    if args.json:
        print(json.dumps(stats, indent=2))
        return

    print("ChatIFU local vault")
    print(f"- Qdrant collection: {stats['collection']}")
    print(f"- Vector chunks: {stats['vector_chunks']}")
    print(f"- Devices: {stats['counts']['devices']}")
    print(f"- Processed SKUs: {stats['counts']['processed_skus']}")
    if stats["processed_statuses"]:
        print("- Processed statuses:")
        for status, count in stats["processed_statuses"].items():
            print(f"  - {status}: {count}")
    if args.deep:
        print(f"- Ollama embeddings: {'ok' if stats['ollama']['ok'] else 'error'}")
        if not stats["ollama"]["ok"]:
            print(f"  - {stats['ollama']['error']}")


if __name__ == "__main__":
    main()
