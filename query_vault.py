from __future__ import annotations

import argparse
from textwrap import shorten

from ollama_client import embed
from vault import search_chunks


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local ChatIFU Qdrant vault.")
    parser.add_argument("question")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--sku")
    parser.add_argument("--source")
    args = parser.parse_args()

    matches = search_chunks(embed(args.question), limit=args.limit, sku=args.sku, source=args.source)
    for index, match in enumerate(matches, start=1):
        print(f"{index}. score={match.score:.4f} sku={match.metadata.get('sku')} source={match.metadata.get('source')}")
        print(f"   {shorten(match.content.replace(chr(10), ' '), width=260, placeholder='...')}")


if __name__ == "__main__":
    main()
