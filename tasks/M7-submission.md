# Submission & Wrap-up

**Grade tie-in:**
Docs, cost & judgement (10%) + protects every other area

## Goal
A submission that is reproducible from an empty AWS account, evidenced, honest,
and leaves nothing running.

## Tasks
- [ ] `README.md`: architecture diagram, `make`-based quickstart from an empty
      account, **decision log** (X over Y because Z; when I'd flip it; deleted
      over-engineering), **cost table** (real per-service estimate under ~$15),
      **known gaps** (what's broken / what I'd fix next).
- [ ] `docs/runbook.md`: one page — "the endpoint is 5xx-ing at 3am, what do I do?"
- [ ] `evidence/`: dashboard screenshot, rollback proof, drift report, one full
      trace (auto-approved + corrected), CI runs.
- [ ] Meaningful git history (not one `initial commit`).
- [ ] Prepare answers to the 7 live-discussion questions (p99 triples; quality
      dropped but drift green; Bedrock model deprecated; why human gate; 4→40 doc
      types; cost doubled; sampling bias after 3 cycles).
- [ ] `make destroy` works; confirm the account is clean.

## Acceptance criteria
- [ ] Full rubric audit: zero instant point-losers; every milestone
      deliverable present in `evidence/`; README claims match the code.
- [ ] Optional but strongly weighted: a 5–10 min walkthrough over the dashboard.

## Definition of done
`make destroy` leaves a clean account and the reviewer's final report is clean.
