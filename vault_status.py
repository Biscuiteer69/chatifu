from __future__ import annotations

from vault import COLLECTION, qdrant, sqlite


def main() -> None:
    conn = sqlite()
    devices = conn.execute("select count(*) from devices").fetchone()[0]
    processed = conn.execute("select count(*) from processed_skus").fetchone()[0]
    conn.close()

    client = qdrant()
    count = client.count(collection_name=COLLECTION, exact=True).count

    print(f"ChatIFU local vault")
    print(f"- Qdrant collection: {COLLECTION}")
    print(f"- Vector chunks: {count}")
    print(f"- Devices: {devices}")
    print(f"- Processed SKUs: {processed}")


if __name__ == "__main__":
    main()

