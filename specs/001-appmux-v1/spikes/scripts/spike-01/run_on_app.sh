# job sent to the harness (/x/sh): __USER__ and __PGHOST__ are replaced on the client at send time
export PGHOST=__PGHOST__
export SPIKE1_USER='__USER__'
export PYTHONPATH=/tmp/spike1/deps
/app/python/source_code/.venv/bin/python /tmp/spike1/obo_matrix.py "${1:-run}" ${2:-}
