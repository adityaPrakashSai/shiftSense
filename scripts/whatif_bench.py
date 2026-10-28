"""Local what-if timing on the seed ward (design doc 6.6, M8 check: median under 0.5 s).

    uv run python -m scripts.whatif_bench [N]

Seeds the default 24-nurse ward, then asks N "why A and not B" questions in local scope: A works
(day, shift) in the published roster, B is another regular nurse; B takes the shift and A is off.
Prints each timing, the verdicts and the median.
"""

import random
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from app.evidence.counterfactual import Question, what_if
from app.store import repo
from app.store.db import open_db
from app.store.seed import seed_db


def main(n: int = 20) -> None:
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "bench.db")
        seed_db(conn, "default")
        ward = repo.get_ward(conn)
        pub = repo.get_published_run(conn, "ward-7")
        assert ward is not None and pub is not None
        grid = repo.get_roster(conn, pub["run_id"])
        regular = [x.id for x in ward.nurses if not x.standby and x.role.value != "ans"]
        cells = [(a, d, s) for a in regular for d, s in enumerate(grid[a]) if s in ("M", "E", "N")]
        times, verdicts = [], Counter()
        for a, d, s in rng.sample(cells, n):
            b = rng.choice([x for x in regular if x != a and grid[x][d] != s])
            t0 = time.perf_counter()
            v = what_if(
                conn, pub["run_id"], Question(forcings=((b, d, s), (a, d, None)), scope="local")
            )
            times.append(time.perf_counter() - t0)
            verdicts[v.verdict] += 1
            print(f"{a}->{b} d{d} {s}: {v.verdict:<10} {v.reason_code:<28} {times[-1]:.3f}s")
        print(
            f"\nlocal what-if on the seed ward: n={n}, median {statistics.median(times):.3f} s, "
            f"p90 {sorted(times)[int(0.9 * n) - 1]:.3f} s, max {max(times):.3f} s; {dict(verdicts)}"
        )
        conn.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
