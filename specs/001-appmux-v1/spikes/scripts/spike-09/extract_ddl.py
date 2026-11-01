"""Spike 9: pull the DDL out of docs/product/v1-data-model.md exactly as written.

usage: extract_ddl.py <data-model.md> <out-dir>
Writes ddl.sql (every ```sql fence in section 4 that holds a CREATE TABLE or an ALTER TABLE, in file order) and grants.sql (the fence in
section 6 that holds the indicative grants). Nothing is edited, so a run shows the errors of the document itself.
"""
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)

section4 = text.split("\n## 4. Tables", 1)[1].split("\n## 5. Walkthroughs", 1)[0]
section6 = text.split("\n## 6. Who writes what", 1)[1].split("\n## 7. Rules enforced in code", 1)[0]
fence = re.compile(r"```sql\n(.*?)```", re.S)

tables = [b for b in fence.findall(section4) if "CREATE TABLE" in b or "ALTER TABLE" in b]
grants = [b for b in fence.findall(section6) if "CREATE ROLE" in b]
(out / "ddl.sql").write_text("\n".join(tables))
(out / "grants.sql").write_text("\n".join(grants))
print(f"{len(tables)} DDL fences, {len(grants)} grants fence")
