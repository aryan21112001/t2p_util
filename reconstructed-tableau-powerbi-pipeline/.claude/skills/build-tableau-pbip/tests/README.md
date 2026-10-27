# Tests

## `Synthetic Coverage Probe.twb` — the corpus-blindness test

A hand-written workbook containing **only features the reference corpus has none of** (see
[`docs/reference-corpus.md`](../../../../docs/reference-corpus.md)). Its purpose is not coverage;
it is to exercise the code paths real input never reached, because a path a corpus cannot reach is
a path that ships untested no matter how many times that corpus is run. This probe is fixed input
checked into the repo, so it stays meaningful whatever corpus you point the build at — though if
your workbooks use features neither it nor the reference corpus contains, those paths are still
unexercised.

```bash
python3 ../scripts/build_pbip.py "Synthetic Coverage Probe.twb" \
  --out /tmp/probe --clean --partitions native
```

What it deliberately contains, and the expected handling:

| Feature | Corpus has | Expected |
|---|---|---|
| `{ INCLUDE … }` / `{ EXCLUDE … }` LOD | 0 | **declined** to L1 — relative to the view's dimensions |
| `{ FIXED … }` LOD | 29 | translated to `CALCULATE(…, ALLEXCEPT(…))` |
| **physical `<relation type='join'>`** with real keys | **0** | reported as `GAP` with the key pair |
| `STR(<date>)` | 0 | **declined** — Tableau emits ISO, DAX coercion is locale-dependent |
| `DATENAME('week', …)` | 0 | **declined** — no unambiguous DAX equivalent |
| `Pie` / `GanttBar` mark classes | 0 | `pieChart` / `ganttChart` |
| `extension` dashboard zone | 0 | reported as `GAP` |
| `web` dashboard zone | 0 | reported as `GAP` |
| `sqlserver` connection | 0 | `Sql.Database(...)` under `--partitions native` |
| range-domain parameter | 0 (all list/any) | table rows generated from min/max/step |
| group, bin, drill-path hierarchy | few | emitted |

Expected result: **0 pre-flight FAIL**, 2/6 calcs translated, 5 residue items
(2 × L1, 2 × L2, 1 × GAP).

## What this file already caught

**A key-based join was silently dropped** — no relationship in `relationships.tmdl` *and* no residue
entry. The physical-join element (`<relation type='join'>`) is different from the logical-layer
`<object-graph>` relationship, and only the latter was being read. The corpus contains **zero**
physical joins, so no amount of running it could have exposed this; `relationships.tmdl` was empty
for the legitimate reason described in SKILL.md, which made the empty file look correct.

It also caught `[Orders].[cust_id]` being reduced to `Orders].[cust_id`, because `.strip("[]")`
removes brackets from the ends of a string while a qualified reference carries them in the middle.

## What this file FAILED to catch, and why -- a cautionary example, not just a success list

`pbir.py`'s mark-class rule table checked `m == "Gantt"`. Tableau's own XML never writes that --
it writes `<mark class='GanttBar' />`. The rule never fired; all 6 real corpus Gantt marks fell to
`tableEx` undecided, invisible unless someone read `RESIDUE.md` and cross-checked the mark name by
hand. This fixture's own `Gantt Sheet` worksheet used `<mark class='Gantt' />` too -- the SAME wrong
assumption, made independently while writing this file, so running the probe exercised nothing
that would have caught it. Found instead by grepping the real corpus `.twb` files directly for
every `<mark class='...'>` value actually written and diffing that list against the rule table --
a check on Tableau's OWN output, not on what this project assumed that output would be. Both the
rule and this fixture are now fixed to `GanttBar`, and a direct case was added to `pbir.py`'s own
`cases` list so the rule is asserted without needing a full build to notice.

The lesson isn't "add a synthetic case for everything" -- it's that a synthetic fixture written
from the same assumption as the code it tests will confirm the assumption, not test it. Whenever
a fixture's correctness rests on "what string does Tableau actually write here", verify it against
a real `.twb` (grep for the literal XML shape) before trusting the fixture, the same way this
project already insists Desktop is the authority over documentation.

Add a case here whenever a Tableau feature is reasoned about but not present in real input. The
whole point is that reasoning about an unexercised path is exactly where this project has been
wrong most often.
