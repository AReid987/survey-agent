"""Get current Chrome window IDs."""
import json, subprocess

def get_window_ids():
    r = subprocess.run(
        ["cua-driver", "call", "list_windows", "--json"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    try:
        windows = json.loads(r.stdout.strip()).get("windows", [])
    except json.JSONDecodeError:
        return {}
    result = {}
    for w in windows:
        if w.get("app_name") == "Google Chrome":
            b = w.get("bounds", {})
            if b.get("height", 0) > 100:
                wid = w.get("window_id")
                title = w.get("title", "")
                pid = w.get("pid")
                result[wid] = {"pid": pid, "title": title, "width": b.get("width", 0), "height": b.get("height", 0)}
    return result

before = get_window_ids()
print(f"Before click: {len(before)} windows")
for wid, info in before.items():
    print(f"  wid={wid} pid={info['pid']} {info['width']}x{info['height']} title=\"{info['title'][:60]}\"")
