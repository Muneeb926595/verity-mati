#!/bin/sh

# Proxy test: starts a backend server, a proxy server, and a client.
# The client talks to the proxy, which forwards to the backend.

client_log=proxy-test-client.log
proxy_log=proxy-test-server.log
server_log=proxy-test-backend.log

rm -f $client_log $proxy_log $server_log

echo "Starting backend server on port 1502"
./unit-test-server > $server_log 2>&1 &
server_pid=$!

sleep 1

echo "Starting proxy server on port 1503"
./proxy-test-server > $proxy_log 2>&1 &
proxy_pid=$!

sleep 1

echo "Starting proxy test client"
./proxy-test-client > $client_log 2>&1
rc=$?

kill $proxy_pid 2>/dev/null
kill $server_pid 2>/dev/null
wait $proxy_pid 2>/dev/null
wait $server_pid 2>/dev/null

if [ $rc -ne 0 ]; then
    echo "PROXY TESTS FAILED"
    echo "--- Client log ---"
    cat $client_log
    echo "--- Proxy log ---"
    cat $proxy_log
    echo "--- Backend log ---"
    cat $server_log
fi

exit $rc
