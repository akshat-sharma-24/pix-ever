"""Download the face models into pix-ever-server/models/, verifying each one.

    python tools/fetch_models.py              # the default model
    python tools/fetch_models.py --all        # every registered model
    python tools/fetch_models.py --force      # re-download even if valid

Everything the download produces is checked against the size and sha256 that
faces.MODELS pins. A truncated or tampered file is reported and discarded
rather than written, because a partly-downloaded recogniser does not crash —
it silently returns wrong vectors, which is close to undebuggable.

Standard library only, so it runs on a bare checkout with nothing installed.
"""

import argparse
import hashlib
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import faces  # noqa: E402

CHUNK = 1 << 20  # 1 MiB


class VerifyError(Exception):
    """A file on disk or just downloaded does not match the registry."""


def _verify(path: str, spec: dict) -> None:
    """Raise VerifyError unless `path` matches the spec's size and sha256."""
    actual_size = os.path.getsize(path)
    if actual_size != spec["size"]:
        short = spec["size"] - actual_size
        detail = f"short by {short}" if short > 0 else f"over by {-short}"
        raise VerifyError(
            f"wrong size: expected {spec['size']} bytes, got {actual_size} ({detail})"
        )

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    if digest.hexdigest() != spec["sha256"]:
        raise VerifyError(
            f"wrong sha256:\n  expected {spec['sha256']}\n  got      {digest.hexdigest()}"
        )


def _download(spec: dict, dest: str) -> None:
    """Stream to dest.part, verify, then rename into place.

    Nothing ever appears at `dest` unless it passed verification, so a failed
    or interrupted run leaves the previous state intact.
    """
    part = dest + ".part"
    try:
        with urllib.request.urlopen(spec["url"], timeout=60) as response:
            with open(part, "wb") as out:
                done = 0
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    pct = done * 100 // spec["size"] if spec["size"] else 0
                    print(f"\r    {done / 1e6:6.1f} / {spec['size'] / 1e6:.1f} MB  {pct:3d}%",
                          end="", flush=True)
        print()
        _verify(part, spec)
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)


def fetch(model_id: str, force: bool = False) -> int:
    """Fetch one model's files. Returns the number that failed."""
    print(f"\n{model_id}")
    os.makedirs(faces.MODELS_DIR, exist_ok=True)
    failures = 0

    for role, spec in faces.MODELS[model_id].items():
        dest = os.path.join(faces.MODELS_DIR, spec["file"])
        print(f"  {role}: {spec['file']}")

        if os.path.exists(dest) and not force:
            # Re-verify rather than trusting presence: this is what catches a
            # file left behind by an older, broken fetch.
            try:
                _verify(dest, spec)
                print("    already present and verified")
                continue
            except VerifyError as e:
                print(f"    present but invalid ({e}); re-downloading")

        try:
            _download(spec, dest)
            print("    verified")
        except VerifyError as e:
            print(f"    FAILED verification: {e}")
            failures += 1
        except (urllib.error.URLError, OSError) as e:
            print(f"    FAILED download: {e}")
            failures += 1

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=faces.DEFAULT_MODEL_ID,
                        choices=sorted(faces.MODELS))
    parser.add_argument("--all", action="store_true",
                        help="fetch every registered model, not just one")
    parser.add_argument("--force", action="store_true",
                        help="re-download even if the file is already valid")
    args = parser.parse_args()

    model_ids = sorted(faces.MODELS) if args.all else [args.model]
    print(f"Models directory: {faces.MODELS_DIR}")

    failures = sum(fetch(model_id, args.force) for model_id in model_ids)

    if failures:
        print(f"\n{failures} file(s) failed. Face tagging will stay disabled.")
        return 1
    print("\nAll files verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
