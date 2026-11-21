"""updater の各スクリプトが同時に git を触らないようにするための鍵。

  from lock import single_run
  with single_run():
      ...

ほかのスクリプトが動いている間は、待たずにそのまま終了する（次の回にまた動く）。
"""
import contextlib
import fcntl
import os
import sys
from pathlib import Path


@contextlib.contextmanager
def single_run():
    state = Path(os.path.expanduser(os.environ.get("STATE_DIR", "~/.sarashina-site")))
    state.mkdir(parents=True, exist_ok=True)
    f = open(state / "run.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ほかの更新が動いているので、今回はスキップします。")
        sys.exit(0)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
