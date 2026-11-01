# Release checklist v0.1.0

## Automated (done in repo)

- [x] `pytest -q` passes with no credentials
- [x] Console screenshots in `docs/images` (setup, rules, board, activity, plan, review)
- [x] Wheel build includes templates and static files
- [x] Dockerfile uses python 3.12 slim, non-root user, and `pam start` entrypoint
- [x] Compose binds `127.0.0.1:8765` and persists the data volume
- [x] Secret scan clean (gitleaks or grep for token patterns)

## Human (do by hand)

- [ ] Make the GitHub repo public
- [ ] Upload the release video (2:30 or less) and add the link to README
- [ ] Submit the hackathon entry
