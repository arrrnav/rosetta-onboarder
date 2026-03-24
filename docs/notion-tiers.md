# Notion Access Tiers — Feature Reference

This document tracks Notion features that this project currently works around, and how upgrading plans would improve the workflow. Useful for future investment decisions or grant/sponsorship applications.

---

## Current Setup (Free Plan)

| Constraint | Workaround in use |
|---|---|
| No webhooks | Poll the "New Hire Requests" DB every 5 minutes for `Status = Ready` rows |
| No native automations | External scheduler (`asyncio` loop / `mcp__scheduled-tasks`) triggers the agent |
| No Notion AI Q&A | Custom Gemini RAG + FastAPI server embedded as an iframe in the wiki page |
| No Custom Agents | Claude + our own agentic loop handles orchestration |
| No link expiry | Wiki links are permanent — revoke access by un-sharing manually |

---

## What Each Tier Unlocks

### Plus ($10/seat/month)

**Webhooks (developer API)**
- Trigger on `page.content_updated`, `comment.created`, `data_source.schema_updated`
- How it helps: replace the 5-minute polling loop with a real-time push event. Agent fires instantly when a team lead adds a new hire row.
- Implementation: expose a `/webhook` endpoint on the chat server; Notion calls it when the DB row is created.

**Public page link expiry**
- Set time-limited share links (e.g. expire after 7 days)
- How it helps: the new hire's wiki link can be automatically revoked after their onboarding window closes. Cleaner security posture.

**Search engine indexing**
- Public pages can be indexed by Google
- How it helps: if onboarding wikis are meant to be findable externally (e.g. for open-source projects), they surface in search.

---

### Business ($20/seat/month)

**Native Database Automations**
- Trigger: "When a page is added to database" or "When property is edited to value X"
- Actions: send Slack notification, update a property, trigger a webhook
- How it helps: zero-infrastructure trigger. Team lead adds a row → Notion's own automation fires our webhook → no polling process to run at all.

**Notion AI Q&A**
- Workspace-wide search assistant (sparkle icon, bottom-right of any page). Searches all pages you have access to, returns cited answers.
- No scoping mechanism: cannot be restricted to a specific page or sub-tree. A new hire's Q&A session would search the entire workspace, not just their wiki.
- No API surface: Notion exposes no endpoint to call Q&A programmatically. Cannot be embedded, automated, or customized per-user.
- Workspace-level system prompt only: one "general instructions" setting applies to all AI features for all users — no per-page or per-hire context.
- Does not replace our RAG server: our Gemini + FastAPI approach is the only way to deliver scoped, per-hire Q&A with a custom system prompt, image retrieval, and public iframe access for non-Notion users. Q&A is additive at best (team leads searching across all wikis), not a substitute.

**Custom Agents (Notion 3.0 — free until May 2026, then Business+)**
- Autonomous agents that run on schedules or triggers, operate on your workspace at DB scale
- How it helps: the `refresh` command (re-fetch GitHub issues/PRs weekly) could be a Notion Custom Agent instead of a CLI command. No external infrastructure needed.
- Current status: free to use until May 3, 2026 for all plan tiers. After that, requires Notion Credits (Business/Enterprise).
- Note: Custom Agents operate *on* Notion — they are not in-page chat widgets. They cannot replace the RAG chat interface.

---

### Enterprise (custom pricing)

**Advanced security**: SCIM user provisioning, workspace analytics, audit logs
- How it helps: for large engineering orgs — auto-provision new hire Notion accounts so they can be added as members and interact with their wiki (comment, edit) rather than view-only.

**Dedicated API rate limits**
- Current limit: 3 requests/sec on all tiers (no differentiation)
- Enterprise may unlock higher throughput for large-scale onboarding (many new hires simultaneously)

---

## Priority Upgrade Path

If investing in a single upgrade, the recommendation is:

1. **Plus** — webhooks alone eliminate the polling loop and make the system truly event-driven at low cost
2. **Business** — native automations + Notion AI Q&A could replace significant custom infrastructure; worthwhile for teams already paying per-seat

For the MLH challenge demo, Free tier + ngrok covers everything needed. The workarounds are functional and the architecture is designed to swap them out cleanly.

---

## API Rate Limits (all tiers, as of 2025)

- **3 requests/second** average, across all integrations on the workspace
- No tier differentiation currently (Notion has stated this may change)
- The wiki generation flow makes ~5–15 Notion API calls per hire — well within limits
- Parallel onboarding of many hires simultaneously could approach limits; add `asyncio.Semaphore` if needed
