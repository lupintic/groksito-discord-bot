"""
Entry point for running Groksito as a module.

Usage:
    python -m groksito_discord
    groksito   (console script after pip install -e .)
"""

from .main import run

if __name__ == "__main__":
    run()
