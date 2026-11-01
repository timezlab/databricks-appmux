"""Spike 1 and 9, local side: run SQL on the Lakebase project as the project owner.

usage: [SPIKE_DB=<database>] owner_sql.py <host> <sql-file>      (statements separated by a line holding only ';;')

The owner's database role and the token are read inside this process: the role from
`databricks current-user me`, the token from `databricks auth token`. Neither is printed;
the role name is masked in the output.
"""
import json
import os
import subprocess
import sys

import psycopg

DBNAME = os.environ.get("SPIKE_DB", "databricks_postgres")  # spike 9 runs in its own database


def cli_json(*args):
    out = subprocess.run(["databricks", *args, "-o", "json"], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def main(host, sql_path):
    token = cli_json("auth", "token")["access_token"]
    user = cli_json("current-user", "me")["userName"]

    def mask(text):
        return str(text).replace(token, "<token>").replace(user, "<owner>")

    statements = [s.strip() for s in open(sql_path).read().split("\n;;\n") if s.strip()]
    with psycopg.connect(host=host, port=5432, dbname=DBNAME, user=user, password=token,
                         sslmode="require", connect_timeout=30, autocommit=True) as conn:
        for sql in statements:
            label = " ".join(sql.split())[:110]
            try:
                cur = conn.execute(sql)
                rows = cur.fetchall() if cur.description else None
                print(f"OK    {label}" + (f"\n      -> {mask(rows)}" if rows is not None else ""))
            except psycopg.Error as e:
                print(f"ERROR {label}\n      -> {type(e).__name__} {e.sqlstate}: {mask(str(e).strip().splitlines()[0])}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
