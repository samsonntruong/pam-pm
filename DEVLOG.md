# DEVLOG

Build log for Pam. Three lines per entry.

## Meta usage

| Product | Component or task | What it did |
|---|---|---|
| Muse Code | T0a minimal skeleton | Created pyproject, CLI, tests placeholder, verified install and pytest |
| Muse Code | T0b skeleton expansion | Created 70 stub files, docs, CI, Docker, manifest copy, DEVLOG table |
| Muse Code | M0 scripts | Created check_meta, check_sam, check_ado, check_slack plus smoke test |
| Muse Spark 1.3 Contributor | M0 check_meta real call | Responses API probe with tool and image, 539 in 113 cached 302 out, cost $0.000103 |
| Muse Spark 1.3 Contributor | M0 check_sam real call | Listed models, confirmed sam-3.1 present, fallback TBD |
| Azure DevOps | M0 check_ado real run | Listed projects, created and deleted test item 1, verified Dependency link names |
| Slack | M0 check_slack real run | Auth and DM pass, app token scope note, button SKIPPED for morning run |

## Entries

### T0a skeleton pass

Created the repo skeleton: stubs for core, models, agents, rules, adapters, and web.
Added docs outline, design copies, CI, Docker files, and community docs.
Verified with pytest, ruff, and `pam --version`; all green.

### M0 connectivity

Meta Responses API is default with chat fallback, tool auto ok, image ok, cached 113 of 539 on cold start.
ADO pass end to end with test item 1 created and deleted and Dependency links verified.
Slack DM pass, app token check needs fix (Web API rejects app tokens, Socket Mode still to verify in morning).

### Spend so far

Muse Code T0b about $0.010 and M0 scripts about $0.014 at Contributor rates, real checks about $0.0001.
Muse Code cached share near 94 percent, real call cold start 21 percent as expected.
Total under $0.05 of $100 cap, no hard stop, continuing to M1.
