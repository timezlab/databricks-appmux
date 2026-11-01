"""Spike 9: turn a plain .sql file into the ';;'-separated form that owner_sql.py runs statement by statement.

usage: split_sql.py <in.sql> <out.sql>
Drops `--` comments and splits on ';'. Safe here because the data model's DDL has no ';' or '--' inside a string literal.
"""
import re
import sys
from pathlib import Path

text = re.sub(r"--[^\n]*", "", Path(sys.argv[1]).read_text())
statements = [s.strip() for s in text.split(";") if s.strip()]
Path(sys.argv[2]).write_text("\n;;\n".join(statements) + "\n")
print(f"{len(statements)} statements")
