"""python -m roster.web"""
import argparse
import sys
from .app import run_server

def main():
    p = argparse.ArgumentParser(prog="roster.web", description="Run the Shift Roster web UI")
    p.add_argument("--host",  default="0.0.0.0",  help="Bind host (default: 0.0.0.0)")
    p.add_argument("--port",  default=5000, type=int, help="Port (default: 5000)")
    p.add_argument("--debug", action="store_true",  help="Enable Flask debug mode")
    args = p.parse_args()
    run_server(host=args.host, port=args.port, debug=args.debug)

if __name__ == "__main__":
    main()
