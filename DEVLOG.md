# DEVLOG

Build log for Pam. Three lines per entry.

## Meta usage

| Product | Component or task | What it did |
|---|---|---|
| Muse Code | T0a minimal skeleton | Created pyproject, CLI, tests placeholder, verified install and pytest |
| Muse Code | T0b skeleton expansion | Created 70 stub files, docs, CI, Docker, manifest copy, DEVLOG table |
| Muse Code | M0 scripts | Created check_meta, check_sam, check_ado, check_slack plus smoke test |
| Muse Code | M1a core | Masking, cost, schemas, store, writes, builder, 8 tests pass |
| Muse Code | M1b adapters client | Fake chat and board, ADO and Slack adapters, Responses ModelClient |
| Muse Code | M1c interviewer | Six turn pushback, structured result, end to end fake pass, 26 tests total |
| Muse Code | M2a rules | Engine, scheduler, poller watermark, morning followup blocker templates |
| Muse Code | M2b state map | Agile Closed vs Scrum Done, close_item, is_closed, check_ado fix |
| Muse Code | M3a web console | Setup wizard, rules, activity SSE, board, cli start doctor, 44 pass |
| Muse Code | M3b review plan | synthesize_review 6000 tokens, propose sprint, vision SAM fallback, 55 pass |
| Azure DevOps | M2 ADO blocker probe | Link PASS, poll PASS, Agile uses Closed not Done, probe items deleted |
| Muse Spark 1.3 Contributor | M0 check_meta real call | Responses API probe with tool and image, 539 in 113 cached 302 out, cost $0.000103 |
| Muse Spark 1.3 Contributor | M0 check_sam real call | Listed models, confirmed sam-3.1 present, fallback TBD |
| Azure DevOps | M0 check_ado real run | Listed projects, created and deleted test item 1, verified Dependency link names |
| Slack | M0 check_slack real run | Auth and DM pass, app token scope note, button SKIPPED for morning run |
| Muse Code | WP-1 slack script fix | App token local prefix check, slack check fully PASS |
| Gitleaks 8.24.2 | WP-1 secret scan | 10 commits scanned, no leaks found |
| Muse Code | WP-1 SAM discovery | Responses shape confirmed live, masks and boxes back on synthetic image |

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

### M1 engine done

Store with evidence log and undo, masking, cost meter, fake adapters, Responses ModelClient.
Interviewer challenges vague answers, six turn cap, structured result, end to end fake pass.
26 tests pass with ruff clean, spend adds about $0.049, total about $0.07, cached share 94 to 95 percent.

### M2 rules and ADO probe

Rules engine with scheduler and poller watermark plus three templates pass 5 fake tests.
Real ADO link and poll pass, Agile state is Closed not Done so templates need a state map.
31 tests pass total, M2a spend about $0.016, total about $0.09, continuing to state fix.

### M3 console review plan done

State map fix pass, web console with wizard rules activity board plus review and plan pass.
Vision uses SAM with fallback to full image read, review uses 6000 tokens or more.
55 tests pass with ruff clean, added spend about $0.071, total about $0.16, cached share 94 to 95 percent.
