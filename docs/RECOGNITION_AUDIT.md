# Recognition audit, 22 October 2026

Baseline: d788f39, PR #2. The initial 65 tests and both Python CI jobs passed; this is not evidence of public CMS support.

| Component | Limitation and consequence | Required change |
|---|---|---|
| detector | Raw regex counts conflate repeated evidence, versions and platforms; WordPress hides wpForo | Independent evidence channels, multiple technologies, versioned capability registry |
| navigation/service | One localized label and one click; unknown GET buttons may mutate state | Bounded structural navigation, explainable scores, action risk classification, menus and dialogs |
| forms | Light DOM only, single score per role; low evidence positional inference | Open shadow traversal, retained alternatives, relational features, conservative ambiguity handling |
| editors | Locator.fill changes DOM but may not update framework state | Editor registry, public editor APIs and input events; network evidence remains authoritative |
| knowledge | Editor/ordinal lookup can transfer a wrong field; no lifecycle, migration or negative evidence | Versioned six-category library, applicability ranking/conflicts, disable/quarantine, transfer evidence, safe import/export/backup |
| capture | Async response reads global action_id after an action changed | Immutable per-request action and redirect ancestry; response timestamps and headers captured before await |
| network | Only adjacent content-bearing writes; final draft commit without content omitted | Dependency DAG across response values, bodies, paths, queries and headers; no timing-only links |
| learner | Dynamic numeric IDs can become constants; no response-header source; observed secrets embedded in URL path | Bind proven dependencies in every location, unknown critical parameters block readiness |
| service gate | All foreign resources blocked, all same-host writes temporarily allowed | Explicit static origins fetched without credentials, exact origin write scope, content/dependency proof and request budget |
| verification | Redirected login and echoed/hidden content can count; result reasons conflated | Independent GET with status/login/error checks, both content roles, original response association |
| runtime | No response-header extraction; host allowlist alone allows port changes; CSS text concatenates block elements | Additive schema-v1 extraction, preserve legacy allowlist semantics but enforce exact origin during learning, whitespace normalization |
| auth | New local key ignores former ~/.poster/session.key | Validate and reuse legacy key without overwriting either key |
| GUI | Same content offered for HTTP duplicate check, no destination selected in advance | Distinct second content, preselected output and automatic save after independent verification |

Further risks: semantic names can disagree with input type; nested editor wrappers can create duplicate forms; malicious background requests share a browser action; content in a login/error page is not a publication; HTML escaping must not persist the training message; knowledge imported from elsewhere is untrusted data, never executable instructions. GET safety cannot be inferred from HTTP method alone. Closed shadow roots, arbitrary client encryption, opaque JavaScript tokens and unbounded workflows require manual learning or a dedicated adapter.

Implementation and measurable validation are reported separately in VALIDATION_V2_DEEP.md. Commercial and uncontrolled public sites are not used for test writes.

## Findings confirmed by the expanded trials

A real phpBB installation exposed duplicate equivalent compose links and session User-Agent binding. A numeric user cookie value happened to equal the forum ID: cookie-source inference therefore now requires a session-sensitive parameter role, not value equality alone. Multi-stage controls exposed stale temporary selector collisions: snapshot rebuilding now clears previous annotations. A negative test name appeared in a form action URL and incorrectly influenced purpose classification: purpose now separates accessible content from exact endpoint segments. Each has a regression or a repeatable integration check.
