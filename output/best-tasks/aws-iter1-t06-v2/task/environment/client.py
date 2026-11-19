#!/usr/bin/env python3
"""Simple TCP client for the replicated KV store."""
import socket
import sys

def send(host, port, cmd, timeout=5.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    s.sendall((cmd + '\n').encode())
    f = s.makefile('rb')
    resp = f.readline().decode().strip()
    s.close()
    return resp

if __name__ == '__main__':
    host = sys.argv[1]
    port = int(sys.argv[2])
    cmd = ' '.join(sys.argv[3:])
    print(send(host, port, cmd))
