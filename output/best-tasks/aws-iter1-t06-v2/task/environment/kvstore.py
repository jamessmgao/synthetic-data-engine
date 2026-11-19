#!/usr/bin/env python3
"""Replicated Key-Value Store with Leader/Follower architecture."""

import asyncio
import json
import os
import sys

import recovery

DATA_DIR = "/app/data"


class WAL:
    """Write-ahead log backed by a file."""

    def __init__(self, path):
        self.path = path
        self.entries = []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.entries.append(json.loads(line))

    def append(self, entry):
        self.entries.append(entry)
        with open(self.path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        return len(self.entries) - 1

    @property
    def last_index(self):
        return len(self.entries) - 1 if self.entries else -1


class KVState:
    """Key-value state machine with apply tracking."""

    def __init__(self, wal_path):
        self.wal = WAL(wal_path)
        self.data = {}
        self.applied_up_to = -1

    def _apply(self, entry):
        op = entry.get('op')
        if op == 'SET':
            self.data[entry['key']] = entry['value']
        elif op == 'DEL':
            self.data.pop(entry['key'], None)

    def apply_through(self, index):
        while self.applied_up_to < index:
            self.applied_up_to += 1
            self._apply(self.wal.entries[self.applied_up_to])

    def get(self, key):
        return self.data.get(key)

    def propose(self, op, key, value=None):
        entry = {'op': op, 'key': key}
        if value is not None:
            entry['value'] = value
        return self.wal.append(entry)


def _load_commit(path):
    if os.path.exists(path):
        with open(path) as f:
            return int(f.read().strip())
    return -1


def _save_commit(path, idx):
    with open(path, 'w') as f:
        f.write(str(idx))


class ReplicatedKV:
    """Server handling client requests and replication."""

    def __init__(self, role, port, peer_host=None, peer_port=None):
        self.role = role
        self.port = port
        self.peer_host = peer_host
        self.peer_port = peer_port
        wal_path = os.path.join(DATA_DIR, f"node_{port}.wal")
        self.commit_path = os.path.join(DATA_DIR, f"node_{port}.commit")
        self.state = KVState(wal_path)
        self.commit_index = -1
        self._recover()

    def _recover(self):
        self.commit_index = _load_commit(self.commit_path)
        target = recovery.get_replay_target(self.state, self.commit_index)
        if target >= 0:
            self.state.apply_through(target)

    async def _handle(self, reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                cmd = line.decode().strip()
                if not cmd:
                    continue
                resp = await self._dispatch(cmd)
                writer.write((resp + '\n').encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, line):
        parts = line.split()
        if not parts:
            return "ERR"
        cmd = parts[0].upper()
        if cmd == 'GET':
            return self._cmd_get(parts)
        elif cmd == 'SET':
            return await self._cmd_set(parts)
        elif cmd == 'DEL':
            return await self._cmd_del(parts)
        elif cmd == 'FLUSH':
            return await self._cmd_flush()
        elif cmd == 'REPLICATE':
            return self._cmd_replicate(parts)
        elif cmd == 'COMMIT':
            return self._cmd_commit(parts)
        elif cmd == 'PROMOTE':
            return self._cmd_promote()
        elif cmd == 'STATUS':
            return self._cmd_status()
        else:
            return f"ERR unknown: {cmd}"

    def _cmd_get(self, parts):
        key = parts[1]
        val = self.state.get(key)
        return f"OK {val}" if val is not None else "NIL"

    async def _cmd_set(self, parts):
        if self.role != 'leader':
            return "ERR_NOT_LEADER"
        key, value = parts[1], parts[2]
        idx = self.state.propose('SET', key, value)
        self.state.apply_through(idx)
        if self.peer_port:
            ok = await self._send_replicate(idx, 'SET', key, value)
            if not ok:
                return "ERR_REPLICATE"
        return f"OK {idx}"

    async def _cmd_del(self, parts):
        if self.role != 'leader':
            return "ERR_NOT_LEADER"
        key = parts[1]
        idx = self.state.propose('DEL', key)
        self.state.apply_through(idx)
        if self.peer_port:
            ok = await self._send_replicate(idx, 'DEL', key)
            if not ok:
                return "ERR_REPLICATE"
        return f"OK {idx}"

    async def _cmd_flush(self):
        if self.role != 'leader':
            return "ERR_NOT_LEADER"
        last = self.state.wal.last_index
        if self.peer_port and last >= 0:
            ok = await self._send_commit(last)
            if not ok:
                return "ERR_COMMIT"
        self.commit_index = last
        _save_commit(self.commit_path, self.commit_index)
        return f"OK commit={self.commit_index}"

    def _cmd_replicate(self, parts):
        idx = int(parts[1])
        op = parts[2]
        key = parts[3]
        value = parts[4] if len(parts) > 4 else None
        entry = {'op': op, 'key': key}
        if value is not None:
            entry['value'] = value
        self.state.wal.append(entry)
        return f"ACK {idx}"

    def _cmd_commit(self, parts):
        idx = int(parts[1])
        self.state.apply_through(idx)
        self.commit_index = idx
        _save_commit(self.commit_path, self.commit_index)
        return f"ACK {idx}"

    def _cmd_promote(self):
        target = recovery.on_promote(self.state, self.commit_index)
        self.role = 'leader'
        self.peer_port = None
        self.peer_host = None
        return "OK"

    def _cmd_status(self):
        return (f"role={self.role} "
                f"applied={self.state.applied_up_to} "
                f"wal_last={self.state.wal.last_index} "
                f"commit={self.commit_index}")

    async def _send_replicate(self, idx, op, key, value=None):
        try:
            r, w = await asyncio.open_connection(self.peer_host, self.peer_port)
            cmd = f"REPLICATE {idx} {op} {key}"
            if value is not None:
                cmd += f" {value}"
            w.write((cmd + '\n').encode())
            await w.drain()
            resp = await asyncio.wait_for(r.readline(), timeout=5.0)
            w.close()
            await w.wait_closed()
            return resp.decode().strip().startswith('ACK')
        except Exception:
            return False

    async def _send_commit(self, idx):
        try:
            r, w = await asyncio.open_connection(self.peer_host, self.peer_port)
            w.write(f"COMMIT {idx}\n".encode())
            await w.drain()
            resp = await asyncio.wait_for(r.readline(), timeout=5.0)
            w.close()
            await w.wait_closed()
            return resp.decode().strip().startswith('ACK')
        except Exception:
            return False

    async def run(self):
        server = await asyncio.start_server(self._handle, '0.0.0.0', self.port)
        async with server:
            await server.serve_forever()


def main():
    role = sys.argv[1]
    port = int(sys.argv[2])
    peer_host = sys.argv[3] if len(sys.argv) > 3 else None
    peer_port = int(sys.argv[4]) if len(sys.argv) > 4 else None
    node = ReplicatedKV(role, port, peer_host=peer_host, peer_port=peer_port)
    asyncio.run(node.run())


if __name__ == '__main__':
    main()
