from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="obsalt",
        description="HTTP ingest server for voice-agent observability",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run("obsalt.api:app", host=args.host, port=args.port, factory=False)


if __name__ == "__main__":
    main()
