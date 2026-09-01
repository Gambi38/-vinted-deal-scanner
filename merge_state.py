#!/usr/bin/env python3
import csv
import json
import sys
from pathlib import Path


def read_seen(path):
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return {str(x) for x in data if str(x).strip()}
    except Exception:
        return set()


def read_alerts(path):
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return [], []
    with p.open('r', encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f)
        rows = list(r)
        return (r.fieldnames or []), rows


def merge_alerts(remote_path, local_path, output_path):
    f1, remote = read_alerts(remote_path)
    f2, local = read_alerts(local_path)
    fields = f1 or f2
    if not fields:
        return

    merged = []
    seen_keys = set()
    for row in remote + local:
        key = (row.get('item_id') or row.get('url') or '').strip()
        if key:
            if key in seen_keys:
                continue
            seen_keys.add(key)
        merged.append(row)

    with Path(output_path).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in merged:
            w.writerow({k: row.get(k, '') for k in fields})


def main():
    if len(sys.argv) != 5:
        raise SystemExit('usage: merge_state.py LOCAL_SEEN LOCAL_ALERTS OUT_SEEN OUT_ALERTS')

    local_seen, local_alerts, out_seen, out_alerts = sys.argv[1:]
    remote_seen = read_seen(out_seen)
    local_seen_set = read_seen(local_seen)
    Path(out_seen).write_text(
        json.dumps(sorted(remote_seen | local_seen_set), indent=2, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    merge_alerts(out_alerts, local_alerts, out_alerts)


if __name__ == '__main__':
    main()
