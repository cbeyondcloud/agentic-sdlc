# Docs site hosting (M3-23)

**For:** the platform team. **When:** once at day 0 ([day-0 bootstrap](day-0-bootstrap.md#the-docs-site-m3-23)),
then with every release.

The docs site is these pages, built from the `docs/` folder of the agentic repository with
[MkDocs](https://www.mkdocs.org/) (BSD licence) and its built-in theme. The Markdown in
`docs/` stays the single source: nothing is copied, and a page changes on the site only
when it changes in the repository.

**What the site does not do.** It has no analytics, loads no web fonts from a font host
and uses no CDN: the theme's CSS, JavaScript, icons and the search index are all in the
built folder. It works on a network with no internet access. A test
(`scripts/tests/test_docs_site.py`) fails the build if a page loads anything from another
host.

## Build it

In a checkout of the release tag, on Windows, macOS or Linux:

```bash
uv sync --group docs
uv run mkdocs build --strict
```

The site is in `site/` (git-ignored): plain `.html` files with relative links, so it works
from any folder of any static server. `--strict` stops the build on a broken link or a
link to a missing heading, so a site you publish never has one. The `docs` dependency
group is optional: it is not part of the CLI or the control plane.

To preview it before you publish: `uv run mkdocs serve`, then open
`http://127.0.0.1:8000`.

## Host it

Any server that serves static files over HTTPS works. Pick the one your org already runs:

| Option | How | Notes |
|---|---|---|
| An internal web server (nginx, Apache httpd, IIS) | copy `site/` to its web root, for example `/srv/agentic-docs/` | the simplest; put it behind your SSO proxy if the docs should not be open to the whole network |
| A container in the cluster | an unprivileged nginx image with `site/` copied in, a Deployment, a Service and an internal Ingress in the `agentic` namespace | follows the control plane's upgrades; pin the image by digest like the chart's images |
| A private object-storage bucket with static website hosting | upload `site/` (any cloud's object store, or an on-premises S3-compatible store) | keep the bucket private and serve it through your internal CDN or proxy with SSO; set `index.html` as the index document |

Whichever you pick:

- **HTTPS only**, with the org's internal certificate.
- **Headers:** `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:`
  works, because the site loads nothing from elsewhere. The two `'unsafe-inline'` are
  needed: MkDocs writes a small inline script on every page (the page's `base_url` and
  the keyboard shortcuts, which differ by folder depth, so a hash list would change with
  the pages) and inline style attributes. A stricter header breaks the menu and search;
  `scripts/tests/test_docs_site.py` checks this header against the built site. Add
  `X-Content-Type-Options: nosniff`.
- **Caching:** `Cache-Control: no-cache` on `.html` files, so a release shows at once; the
  CSS, JavaScript and fonts can be cached for a day.
- **Access logs:** your server's own logs are the only record of who read what; the site
  sends nothing anywhere.

## Update it with each release

Part of the [release runbook](release.md)'s rollout: build from the new release tag and
replace the whole folder (not a merge, so a removed page goes away). Then check:

1. the start page opens and its role links work;
2. the [command reference](../guides/command-reference.md) shows the new release's commands;
3. the search box finds `agentic init`.

## If the build fails

| Message | Fix |
|---|---|
| `contains a link to 'x.md' which is not found` | fix the link in the Markdown, by pull request; never publish without `--strict` |
| `contains a link '...#heading', but there is no such anchor` | the heading was renamed: link to its new name |
| `No module named mkdocs` | `uv sync --group docs` |
| a new page is missing from the menu | add it to `nav:` in `mkdocs.yml` (the test fails until you do) |
