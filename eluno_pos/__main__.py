"""Run the local Eluno POS application shell."""

import argparse

from .http_app import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Eluno POS application")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    print("Eluno POS application on http://{}:{}".format(args.host, args.port))
    serve((args.host, args.port))


if __name__ == "__main__":
    main()

