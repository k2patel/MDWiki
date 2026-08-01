# MDWiki

MDWiki turns a directory of Markdown files into a contemporary, searchable wiki. Content stays outside the image on a persistent volume; MDWiki rebuilds navigation, topic pages, and full-text search when files change.

The application is MIT licensed, runs as a non-root distroless container, supports system light/dark appearance, and keeps common DokuWiki and clean page URLs working.

## What is included

- Automatic topic classification and `/topics/` index
- Browser full-text search generated from all Markdown pages
- Clean URLs such as `/pianobar` and `/title/Pianobar`
- Redirects for `doku.php?id=wiki:syntax` and legacy media endpoints
- Optional Basic-authenticated page creation and Markdown upload at `/admin`
- Runtime branding, homepage text, topic labels, repository links, and light/dark colors
- Helm deployment with a content PVC, ephemeral build volume, probes, and restricted security context
- Generic DokuWiki-to-Markdown migration tool

## Local development with a venv

Create content outside the tracked application files:

```bash
mkdir -p content
printf '# My MDWiki\n\nHello from Markdown.\n' > content/index.md
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
MDWIKI_CONTENT_DIR="$PWD/content" \
MDWIKI_SITE_DIR="$PWD/.runtime/site" \
MDWIKI_CONFIG="$PWD/mkdocs.yml" \
MDWIKI_ADMIN_FILE="$PWD/admin/index.html" \
python server.py
```

Open <http://127.0.0.1:8080>. Set `MDWIKI_ADMIN_PASSWORD` to enable `/admin`; it is disabled by default.

## Container

```bash
docker build -t mdwiki:local .
docker run --rm -p 8080:8080 \
  -v "$PWD/content:/data/mdwiki" \
  --tmpfs /tmp \
  -e MDWIKI_SITE_NAME="My Wiki" \
  mdwiki:local
```

The image contains the renderer, theme, and server only. It never copies `data/`, `docs/`, or another wiki's pages. Generated HTML is built under `/tmp`; Markdown remains in `/data/mdwiki`.

To enable browser publishing, provide credentials through secrets rather than baking them into the image:

```bash
-e MDWIKI_ADMIN_USER=editor -e MDWIKI_ADMIN_PASSWORD='use-a-secret'
```

## Kubernetes

```bash
helm upgrade --install mdwiki helm/mdwiki \
  --namespace mdwiki --create-namespace \
  --set image.repository=ghcr.io/k2patel/mdwiki \
  --set image.tag=latest
```

The chart creates a 2 GiB `ReadWriteOnce` PVC by default. Use `persistence.existingClaim` for an existing volume, or select a storage class with `persistence.storageClass`. MDWiki defaults to one replica because a single writable content volume and synchronous rebuilds are the safest portable behavior.

Enable uploads with a Kubernetes Secret:

```bash
kubectl -n mdwiki create secret generic mdwiki-admin \
  --from-literal=username=editor \
  --from-literal=password='use-a-long-random-secret'

helm upgrade --install mdwiki helm/mdwiki \
  --namespace mdwiki \
  --set admin.existingSecret=mdwiki-admin
```

All visual identity is configured below `branding` in `helm/mdwiki/values.yaml`. The default palette has WCAG-friendly contrast in automatic light and dark modes.

## Import DokuWiki content

The migration tool is standard-library-only and accepts any DokuWiki pages/media tree:

```bash
python tools/convert_dokuwiki.py \
  --source-pages /path/to/dokuwiki/data/pages \
  --source-media /path/to/dokuwiki/data/media \
  --output content \
  --report /tmp/conversion-report.json
```

Copy the resulting `content/` directory into the MDWiki PVC using a temporary utility pod, CSI volume tooling, or your normal storage workflow. Do not add private content to the application repository.

## Branding environment variables

`MDWIKI_SITE_NAME`, `MDWIKI_SITE_DESCRIPTION`, `MDWIKI_COPYRIGHT`, `MDWIKI_HERO_EYEBROW`, `MDWIKI_HERO_TITLE`, `MDWIKI_HERO_ACCENT`, `MDWIKI_HERO_DESCRIPTION`, `MDWIKI_TOPICS`, `MDWIKI_PRIMARY_LIGHT`, `MDWIKI_PRIMARY_DARK`, `MDWIKI_ACCENT_LIGHT`, `MDWIKI_ACCENT_DARK`, `MDWIKI_REPO_URL`, `MDWIKI_REPO_NAME`, and `MDWIKI_EDIT_URI` can be set directly or through the Helm chart.

## Validate

```bash
python3 -m unittest discover -s tests -v
helm lint helm/mdwiki
docker build -t mdwiki:local .
```

MDWiki is available under the [MIT License](LICENSE).
