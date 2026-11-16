from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    # Runs in an isolated child process so Tk owns its main thread.
    import tkinter as tk
    from tkinter import filedialog

    if len(sys.argv) < 4:
        return 2
    mode = sys.argv[1]
    title = sys.argv[2]
    filetypes = json.loads(sys.argv[3])
    initial = sys.argv[4] if len(sys.argv) > 4 else ""
    initial_path = Path(initial).expanduser() if initial else None
    kwargs = {
        "title": title,
        "filetypes": [tuple(item) for item in filetypes],
    }
    if initial_path:
        kwargs["initialdir"] = str(initial_path if initial_path.is_dir() else initial_path.parent)
        if not initial_path.is_dir():
            kwargs["initialfile"] = initial_path.name

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        if mode == "directory":
            result = filedialog.askdirectory(title=title)
        elif mode == "save":
            result = filedialog.asksaveasfilename(**kwargs)
        else:
            result = filedialog.askopenfilename(**kwargs)
    finally:
        root.destroy()
    if result:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
