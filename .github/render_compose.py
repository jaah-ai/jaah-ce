#!/usr/bin/env python3
"""Render the release's compose.yml: each Jaah image pinned to the digest the release workflow just signed.

    render_compose.py <template> <version> <images> api=sha256:… web=sha256:… node=sha256:… > compose.yml

<images> is the image-name prefix the release pushed (`ghcr.io/<owner>/<repo>`). Every `image:` line of the
template's Jaah services (`…-<name>:${JAAH_VERSION:-dev}`) becomes `${JAAH_CE_IMAGES:-<images>}-<name>:<version>@<digest>`.
Fails, printing nothing, when a digest is missing or malformed, a pair names no image line, or any `image:` line is
left without an `@sha256:` digest. Standard library only: the release runner has a bare Python.
"""
import re
import sys

CE_IMAGE = re.compile(r"^(?P<indent>\s*image: )\$\{JAAH_CE_IMAGES:-[^}]+\}-(?P<name>[a-z]+):\$\{JAAH_VERSION:-dev\}\s*$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def render(template: str, version: str, images: str, digests: dict[str, str]) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"not a release version: {version}")
    bad = sorted(name for name, digest in digests.items() if not DIGEST.match(digest))
    if bad:
        raise ValueError(f"not a sha256 digest: {', '.join(bad)}")
    lines, rendered = [], set()
    for line in template.splitlines(keepends=True):
        match = CE_IMAGE.match(line)
        if match:
            name = match["name"]
            if name not in digests:
                raise ValueError(f"no digest for the {name} image")
            line = f"{match['indent']}${{JAAH_CE_IMAGES:-{images}}}-{name}:{version}@{digests[name]}\n"
            rendered.add(name)
        lines.append(line)
    if unused := sorted(set(digests) - rendered):
        raise ValueError(f"no image line for: {', '.join(unused)}")
    out = "".join(lines)
    if unpinned := [l.strip() for l in out.splitlines() if re.match(r"\s*image:", l) and "@sha256:" not in l]:
        raise ValueError(f"image line left without a digest: {unpinned[0]}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 4 or not all("=" in pair for pair in argv[3:]):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    template, version, images = argv[:3]
    digests = dict(pair.split("=", 1) for pair in argv[3:])
    try:
        with open(template, encoding="utf-8") as f:
            out = render(f.read(), version, images, digests)
    except (OSError, ValueError) as e:
        print(f"render_compose: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
