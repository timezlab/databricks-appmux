# job sent to the harness: starts the soak detached so the job returns at once; __USER__ and __PGHOST__ are replaced on the client
export PGHOST=__PGHOST__
export SPIKE1_USER='__USER__'
export PYTHONPATH=/tmp/spike1/deps
rm -f /tmp/spike1/soak.log
setsid nohup /app/python/source_code/.venv/bin/python /tmp/spike1/obo_soak.py /tmp/spike1/soak.log > /tmp/spike1/soak.out 2>&1 < /dev/null &
sleep 3
cat /tmp/spike1/soak.log /tmp/spike1/soak.out 2>&1 | head
