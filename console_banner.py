from __future__ import annotations

import os
import sys


NEUNGGUREONGI_ART = r"""
                         __
                    _.-'  `-._
                 .-'          `-.
               .'    _..---.._   `.
              /    .'  _   _  `.   \
             ;    /   (o) (o)   \   ;
             |   |       ^       |  |
             ;    \   ._____.   /   ;
              \    `._\_____/_.`   /
               `._       _      _.'
                  `--.._  `----'
                         \ \
                    _.-~~   ~~-._
                 .-~             ~-.
                /    _.-~~~~~-._    \
               ;   .'           `.   ;
                \  \             /  /
                 `._`-._     _.-'_.'
                    `--.~~~~~.--'
                        `---'
"""


def print_server_banner() -> None:
    """Print the server splash once when the FastAPI application starts."""
    use_color = sys.stdout.isatty() and not os.getenv("NO_COLOR")
    green = "\033[38;2;83;170;95m" if use_color else ""
    gold = "\033[38;2;231;181;70m" if use_color else ""
    reset = "\033[0m" if use_color else ""
    port = os.getenv("APP_PORT", "8000")
    print(f"{green}{NEUNGGUREONGI_ART}{reset}", flush=True)
    print(f"{gold}  PROJECT NEUNGGUREONGI  |  OGC MAP SERVER{reset}", flush=True)
    print("  ------------------------------------------------", flush=True)
    print(f"  Layer Admin : http://127.0.0.1:{port}/admin/layers", flush=True)
    print(f"  API Docs    : http://127.0.0.1:{port}/docs", flush=True)
    print("  Services    : WMS / WFS / WCS / WPS", flush=True)
    print("  ------------------------------------------------\n", flush=True)
