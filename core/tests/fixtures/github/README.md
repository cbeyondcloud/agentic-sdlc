# Recorded GitHub REST responses

Response bodies in the shape GitHub's REST API (version 2022-11-28) returns them, used
by the D33 branch-protection check (M3-09) instead of live calls:

| File | Endpoint | Status |
|---|---|---|
| `protection-code-owners.json` | `GET /repos/{owner}/{repo}/branches/{branch}/protection` | 200 |
| `protection-no-code-owners.json` | same | 200 |
| `protection-not-protected.json` | same | 404 |
| `protection-no-admin.json` | same, token without admin rights | 403 |
| `rules-code-owners.json` | `GET /repos/{owner}/{repo}/rules/branches/{branch}` | 200 |
| `rules-no-review.json` | same | 200 |

URLs and ids are those of a made-up `acme/agentic-org-policy` repository.
