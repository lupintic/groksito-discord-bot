"""
Entry point for running Groksito as a module.

Usage:
    python -m src.groksito_discord
    or
    python -m groksito_discord   (after proper packaging)
"""

from .bot import run

if __name__ == "__main__":
    run()
