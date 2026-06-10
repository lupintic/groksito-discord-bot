"""One-time cleaner for leftover skill schema text in tools.py after Chunk 2 extraction."""
from pathlib import Path
import re

path = Path("src/groksito_discord/tools.py")
content = path.read_text(encoding="utf-8")

# Strategy: locate the comment that was left from the bad paste and the next real def get_continuation_tools
# and cut everything between them.

marker = "# (All skill meta schemas"
start = content.find(marker)
end = content.find("def get_continuation_tools(")

if start != -1 and end != -1 and start < end:
    before = content[:start]
    after = content[end:]
    cleaned = before + "\n# (All skill meta schemas, custom schemas, helpers and handlers extracted to skill_tools.py — imported at top of this file.)\n\n" + after
    path.write_text(cleaned, encoding="utf-8")
    print(f"Cleaned leftover block from line ~{content[:start].count(chr(10))+1}")
else:
    print("Markers not found or order wrong. No change made.")
    # As last resort, remove any obvious orphaned """ or schema-looking text between light schema and continuation
    # (we already tried precise; if this runs it means manual review may be needed)
print("Done.")