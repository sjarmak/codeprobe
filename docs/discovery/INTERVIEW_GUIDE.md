# Discovery Interview Guide — 90 minutes

> Script for the Phase 0 D1 partner interview. The six template sections in
> `TEMPLATE.md` are filled in from this conversation; every numbered step
> below maps to a template section. Run the script in order — don't
> improvise. Comparability across the three partner interviews depends on
> a fixed running order.

## Before the interview

1. Confirm the partner has signed off on the interview scope in writing.
   Record the consent reference out-of-repo.
2. Confirm which repo the partner plans to contribute for the dogfood mine
   (D2). You don't need the repo in the room, but you do need its approximate
   size, language, and team shape.
3. Pre-read the partner's public engineering content (blog, talks, public
   repos). Ten minutes of homework avoids asking them to explain the basics.
4. Pin this document in the notes doc. Every numbered step below gets a
   short paragraph of notes — fill them in live.
5. Confirm the sanitization process with the partner up front: "we'll draft a
   sanitized summary, share it with you for redactions, and merge it only
   after you sign off." Nobody should be surprised what ends up public.

## Opening — 5 minutes

1. **Introductions (2 min).** Briefly: who you are, who the partner is,
   what codeprobe is trying to do (`a benchmark pipeline for AI coding
   agents on enterprise repos, not OSS repos`), what Phase 0 is
   (`we're gating Phase 1 on three of these interviews so we don't
   build the wrong thing`).
2. **Consent re-confirm (1 min).** "We're recording this conversation
   to transcribe locally. The transcript doesn't leave our environment;
   the committed artifact is sanitized and you sign off on it before
   it merges. Ok?" Wait for explicit ok.
3. **Agenda preview (2 min).** Walk the interviewee through the six
   template sections so they know where we're going. Share the template
   link if they want to read ahead.

## Section 1 — Evaluation Practice (15 minutes)

4. **Current agent inventory.** "What AI coding agents do your engineers
   have access to today — in production, in pilot, or as experiments
   that didn't land?" Expect a list of 2–5.
5. **Who decides what's in that list.** "Who decides which agents reach
   your engineers? Security? Procurement? An individual VP? A community
   of practice?" The answer drives who we write the decision-maker
   artifacts for.
6. **What "working" looks like today.** "When an agent in that list is
   working well, how do you know? What's the leading indicator?"
7. **What "not working" looks like today.** "And when one stops working
   or underperforms — what's the *first* signal you see? Dashboards?
   Engineer complaints? A quiet drop in usage?"
8. **Failed pilots.** "Have you had an agent pilot end because the
   numbers didn't mean anything, versus because of budget or
   compatibility? If yes — tell me about that one."
9. **Wish list.** "What would you measure if you could, but can't
   today?" This is the highest-signal question in the section. Write
   down the verbatim answer.
10. **Quote capture.** Before moving on, read back 2–3 candidate quotes
    and confirm they're ok to reproduce (redacted) in the committed
    artifact.

## Section 2 — Decision-Maker Artifacts (15 minutes)

11. **Who consumes evaluation output.** "Outside of the engineers actually
    using the agent — who looks at evaluation numbers? Name the roles,
    not the people." Expect VP Eng, platform lead, procurement, FinOps,
    security, maybe a board committee.
12. **Artifact per role.** For each role named: "What artifact does
    that role look at? A Datadog dashboard? A Sigma workbook? A Google
    Sheets sent monthly? A SOC-2 control report? An internal portal?"
13. **Freshness and cadence.** "How fresh does the data need to be?
    Is last week ok? Yesterday? Real-time?" Write it down per artifact.
14. **Where codeprobe needs to land.** "If codeprobe produced a bundle
    of JSON and an HTML viewer, would that reach this decision-maker?
    If not — what's the adapter that closes the gap?" Capture the
    partner's ranked list of must-have exporters (Datadog / Sigma /
    Sheets / custom).
15. **Procurement gate.** "Is there a procurement or security checklist
    that gates which tools your engineers can evaluate? If yes — would
    codeprobe need to clear that gate before a pilot?"
16. **Data residency.** "Any data-residency constraints we should know
    about? EU-only, US-only, region-locked?"

## Section 3 — Benchmark Definition (10 minutes)

17. **Internal vocabulary.** "Is 'benchmark' a noun you already use
    internally? What does it mean when you use it?" Listen carefully —
    the answer changes how `codeprobe interpret` needs to surface.
18. **Candidate framings.** Walk through the candidate framings: fixed
    test suite, continuous eval on production PRs, rubric applied by
    senior engineers, dashboard of incidental metrics, "we don't have
    a word for this." Ask which is closest. Let them pick more than
    one; capture the ranking.
19. **Reward preference.** "If codeprobe handed you a score per task,
    would you want it binary (pass/fail), continuous (weighted partial
    credit), or rubric-based (model scoring on dimensions)?" Capture
    constraints — "security won't approve rubric" is a first-class
    input.
20. **Statistical sophistication.** "Would you want to see confidence
    intervals, p-values, effect sizes — or just 'config A is better'?"
    Some partners want the full McNemar treatment; others want one
    number.

## Section 4 — Taxonomy Mapping (15 minutes)

21. **Walk the CSB/EB taxonomy live.** Read each task type aloud —
    `crossrepo`, `understand`, `refactor`, `security`, `feature`,
    `debug`, `fix`, `test`, `document` — and for each: "Roughly
    what fraction of your engineering work looks like this?" Write
    down their fraction verbatim — "nearly everything", "some", "rare",
    "never", or a percentage if they volunteer one.
22. **Probe for gaps.** "Is there a category of work your engineers
    spend time on that doesn't fit any of those nine buckets?" Stay
    quiet — the premortem's candidate list (`dependency_upgrade`,
    `framework_migration`, `config_surgery`, `runbook_execution`,
    `observability_change`, `compliance_patch`) is worth prompting with
    only if they don't surface anything on their own.
23. **Frequency check.** For each gap they surface: "How often does
    that come up? Once a sprint? Once a quarter?"
24. **Ground-truth check.** For each gap: "When one of these is done,
    where does the record live? PR, ticket, runbook, Slack?" This
    decides whether R3-new can mine the task type from the partner's
    existing artifacts or whether we'd have to synthesize it.
25. **Representative example.** Pick the most frequent gap and ask for
    one concrete (sanitized) example. A real example from their work
    is the best ground truth for the task-type spec.

## Section 5 — Non-PR Artifacts (15 minutes)

26. **RFC / design docs.** "Do your engineers write RFCs or design docs?
    Where do they live — Markdown in a repo, Notion, Confluence, Google
    Docs? Is there a template?"
27. **Ticket system.** "Jira, Linear, Shortcut, something bespoke?
    Where does the engineering rationale actually live — epic
    description, ticket body, comments, linked designs?"
28. **Chat.** "Which Slack or Teams channels carry durable engineering
    rationale? Are archives searchable? Is participation in an
    engineering decision expected to leave a chat trail?"
29. **CODEOWNERS / churn.** "Do you use CODEOWNERS? Is file churn
    already a signal your review process uses?"
30. **CI history.** "How far back does your CI failure history go?
    Is it accessible by API or only through the CI tool's UI?"
31. **Runbooks.** "Where do runbooks live? Who owns updating them?"
32. **Adapter ranking.** Close the section with: "Of those artifacts we
    just walked through — if codeprobe could mine exactly two of them
    as ground truth for benchmark tasks, which two would help you most?"
    Capture the ranked answer verbatim.

## Section 6 — Security Trust Boundary (10 minutes)

33. **Data out.** "When codeprobe reads a repo, the first question your
    security team will ask is: what leaves the environment? Let me walk
    through what codeprobe reads and what it persists — stop me where
    this would be a problem." Walk: source bytes → prompt → LLM API →
    trace → snapshot.
34. **LLM backends.** "Which LLM backends are approved in your
    environment?" Capture the list: Anthropic public, Bedrock, Vertex,
    Azure OpenAI, on-prem vLLM, none-of-the-above. For each, note any
    terms (region, version, data-use policy).
35. **Trace persistence.** "Would a durable agent trace containing
    source bytes be acceptable, or would that trace need to live on your
    infrastructure only?" Feeds R5 and R14.
36. **Redaction default.** "Our default is `hashes-only` — zero source
    bytes in any exported snapshot. Would that default work for you?
    Or do you need something stricter?"
37. **Tenant isolation.** "codeprobe namespaces state by a `tenant_id`
    you supply. What would you want that identifier to look like?"
38. **Execution posture.** "Agent execution runs in a container with a
    read-only bind mount against the worktree. Do you use Docker?
    Podman? Neither?"
39. **Approval path.** "Who on your side would sign off on codeprobe
    for a pilot, and roughly how long would that take? What artifacts
    would they need — a one-pager, a diagram, a SOC-2 control map?"
40. **Outright blockers.** "Is anything we've described an outright
    blocker for your environment?" Capture verbatim.

## Closing — 5 minutes

41. **Recap the six sections.** Briefly reflect what you heard on each —
    "evaluation practice today, decision-maker artifacts, benchmark
    definition, taxonomy mapping, non-PR artifacts, security trust
    boundary" — and ask the interviewee to correct anything mis-stated.
42. **Dogfood scheduling.** "For D2 we'd like to run current codeprobe
    against the repo you're contributing. What's the process — do you
    mirror it for us, or do we run it in your environment? Who should
    we coordinate with?"
43. **Sample-rating offer.** "Optionally — can a staff engineer on your
    side rate 5 sample mined tasks once we have them, on a 1–5
    'representative of our real work' scale? That gives us the partner
    validation signal in the PRD's Exit Criteria."
44. **Sanitization expectation.** "We'll draft the sanitized summary
    within a week, share it with you, and merge only after you sign
    off. Good?"
45. **Follow-up questions.** Read back any questions the interviewee
    raised that you couldn't answer in the room, and commit to
    responding within one week.
46. **Thanks and close.** Ten seconds of sincere thanks. Their time is
    the most expensive input to this project.

## After the interview

47. **Within 24 hours.** Fill in `docs/discovery/TEMPLATE.md` →
    `docs/discovery/<partner-slug>.md` from the transcript. Do not
    rely on memory after 24 hours — the quality of the artifact drops
    fast.
48. **Within one week.** Share the sanitized draft with the partner
    for redactions. Accept every redaction they request without
    argument.
49. **After partner sign-off.** Open a PR landing the sanitized file.
    The PR description includes the signed-off date and the hash of
    the consent record.
50. **Follow-up responses.** Answer every followup question the partner
    raised in the room. Close the loop.
51. **Aggregation.** Once three partner files exist, produce
    `taxonomy-delta.md` aggregating the new task types and the
    decision-maker artifact preferences. This file closes Phase 0 and
    unblocks Phase 1.

## Anti-patterns

52. **Don't sell codeprobe.** This is a discovery interview, not a
    pitch. Every minute you spend explaining what codeprobe does is
    a minute you don't get to hear what the partner actually needs.
53. **Don't skip sections.** The six sections are comparable across the
    three partners because they're run identically. Skipping "Security
    Trust Boundary" because the interviewee is a staff engineer and not
    a CISO means `taxonomy-delta.md` has a gap exactly where the PRD
    most needs input.
54. **Don't debate taxonomy live.** If the partner says `crossrepo`
    doesn't describe their work, write that down — don't try to
    convince them otherwise. The point is to capture their mental model,
    not to defend ours.
55. **Don't let the sanitization pass be an afterthought.** A
    sanitization pass done in a rush produces a file that gets quietly
    deleted three months later when a leak is caught. Do it carefully,
    with the partner present.
