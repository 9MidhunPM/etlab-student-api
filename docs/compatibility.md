# Institution compatibility

This API has a generic adapter for the ETLab student web portal. Institutional navigation, route names, enabled modules, account permissions, and login flows vary. The public resource list describes the interface, not a claim that every college exposes every feature.

| Target | Evidence | Status |
| --- | --- | --- |
| Generic label-preserving HTML parsing | Synthetic/sanitized fixture tests and mocked responses | Fixture coverage; not a production institution certification |
| Sahrdaya (`https://sahrdaya.etlab.in`) | Updated credentials authenticated through the real default adapter; opt-in live test passed five core base resource reads | Five base reads live verified; remaining coverage listed below |
| Other `*.etlab.in` institutions | Host policy permits them; adapters discover navigation per session | Unverified until tested with an authorized account |
| Custom ETLab domains | Explicit hostname allowlist required | Unverified until login/layout compatibility is tested |

The implementation does not rely on an assumed ETLab mobile API or publish undocumented native API claims. It reads observed web pages and their read-only forms. A generic parser cannot promise complete semantics or coverage for layouts never seen in testing.

See the dated [verification record](verification.md) for automated checks and the separate limits on authenticated portal, browser, Docker, and deployment evidence.

Outbound network access became available during verification, and updated authorized credentials resolved the earlier login rejection. The DNS/connection failures and rejected credentials are historical attempts. No other institution has been live verified.

## Sahrdaya resource evidence

| Resources | Observed evidence | Status |
| --- | --- | --- |
| `profile`, `subjects`, `timetable`, `attendance`, `results` | Opt-in personal-mode test passed real-upstream base fetches, response schemas/titles, privacy/request-ID headers, cache-repeat timestamp checks, and session inspection/deletion | Live base resource/contract smoke verified |
| `assignments`, `materials`, `notices` | Navigation links discovered for the authorized account | Discovery only; reads, filters, details, and downloads unverified |

These student-data observations used personal mode. The [hosted multi-user service](https://etlab-api.midhunpm.in/docs) has separately passed HTTPS, container-health, public-documentation, and authentication-boundary checks. Hosted student login/reads, alternate institutions, filters, detail pages, attachment downloads, and comparisons with protected portal GUI values remain unverified. A successful base page is not verification of its linked workflows. Runtime capability statuses remain discovery metadata; they do not automatically promote a feature after a documentation verification run.

## Capability status

Call `GET /v1/capabilities` after login. Each entry has `resource`, `available`, `status`, and an optional `reason`:

| Status | Meaning |
| --- | --- |
| `discovered` | An accessible-looking module link was found for the current account. Parsing and all subpages are not automatically verified. |
| `fixture_tested` | The stated behavior has been exercised using a representative sanitized fixture. |
| `live_verified` | The stated behavior was checked against an authorized live portal account. |
| `unavailable` | The module was not exposed to this account or the adapter cannot identify a supported route. |
| `unverified` | There is no adequate verification evidence for the feature. |

Runtime discovery is not the same as the repository's verification matrix. A student may have no records in a usable feature, or permission may be denied after following its link. Empty data returns `data.empty=true` when the portal explicitly indicates no records. Missing features and parsing failures return distinct errors.

## Resource coverage to validate

Validate each module the account exposes: profile/dashboard; semesters/subjects/timetable; attendance; results/assessments; assignments/materials; notices/calendar; fees/receipts; leave/library/placement/surveys/documents. Check filters, detail pages, and file downloads independently; a working list page does not establish those paths.

When adding verification evidence, record the date, institution, adapter version, resource, sample scenario, and result without storing student data. Use an anonymous account description such as “student account with current-semester records.” Do not mark another college as supported based only on a successful Sahrdaya run.
