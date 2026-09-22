"""Accuracy and speed harness: decides int8 vs fp32, and the match threshold.

    python test/compare_faces.py test/testdata/people        # enrol + test
    python test/compare_faces.py some_photo.jpg         # one photo's faces

LAYOUT

    test/testdata/people/
      ada/        ada-r1.jpeg ...     <- reference photos, enrolled
      brij/       bri-r1.jpeg ...
      chandra/    cha-r1.jpeg ...
      ada-t1.jpeg                     <- test photos, loose at the top level
      ada-bri-t1.jpeg
      cha-t2.jpeg

Per-person folders are enrolment: they stand in for what the phone uploads to
POST /people, so the dominance rule applies and each contributes exactly one
subject face.

Loose files at the top level are the library being scanned. Every face in
them is detected and tagged, because a photo can contain several people and
all of them should end up searchable.

GROUND TRUTH comes from the filename. Split the name on "-", drop a trailing
tN, and each remaining token must be a prefix of exactly one person folder:

    ada-bri-t1.jpeg  ->  ada + brij
    cha-t2.jpeg      ->  chandra

A token matching no person, or more than one, is reported and that photo is
left out of the scoring rather than guessed at.

WHAT IS MEASURED is the row that would land in FileTags: for each test photo,
which people get tagged. A tag is right, wrong or missing, and wrong is the
expensive kind — a missing photo is merely absent from a search, a wrong one
shows up in someone else's.

Nothing here is used by the server. It writes no files and changes no state.
"""

import argparse
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import faces  # noqa: E402

try:
    import numpy as np
except ImportError:
    sys.exit("numpy is required: pip install -r requirements-faces.txt")

DOMINANCE_RATIO = faces.DOMINANCE_RATIO
THRESHOLDS = [round(t, 3) for t in np.arange(0.20, 0.76, 0.01)]
REPORT_AT = [0.30, 0.35, faces.COSINE_THRESHOLD, 0.40, 0.45, 0.50, 0.55, 0.60]


# --- loading ---

def load_references(engine, root, dominance, force_largest, problems):
    """{person: [(filename, Face)]} from the per-person folders.

    One face per photo. A folder name says *who*, not *which face*, so when
    two faces are comparable in size there is no honest way to tell which one
    the folder means and the photo is skipped. Taking the biggest regardless
    is how a reference set ends up holding the wrong person's face.
    """
    refs = {}
    for person in sorted(os.listdir(root)):
        folder = os.path.join(root, person)
        if not os.path.isdir(folder) or person.startswith("."):
            continue
        entries = []
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not faces.is_supported(path):
                continue
            try:
                found = engine.faces_in_image(path)
            except ValueError as e:
                problems.append(f"ref {person}/{name}: {e}")
                continue
            # Same rule the server applies at POST /people, so a photo the
            # harness accepts is never one enrolment would reject.
            try:
                subject = faces.subject_face(found, dominance)
            except faces.NoSubjectFace as e:
                if not force_largest:
                    problems.append(f"ref {person}/{name}: SKIPPED — {e}")
                    continue
                subject = max(found, key=faces.face_size)
                problems.append(f"ref {person}/{name}: forced largest — {e}")
            entries.append((name, subject))
        if entries:
            refs[person] = entries
    return refs


def parse_expected(filename, people):
    """(expected, unresolved) — who the filename says is in this photo."""
    stem = os.path.splitext(filename)[0]
    tokens = [t for t in re.split(r"[-_]", stem) if t]
    while tokens and re.fullmatch(r"t\d*|\d+", tokens[-1], re.I):
        tokens.pop()

    expected, unresolved = [], []
    for token in tokens:
        matches = [p for p in people if p.lower().startswith(token.lower())]
        if len(matches) == 1:
            if matches[0] not in expected:
                expected.append(matches[0])
        elif not matches:
            unresolved.append(f"'{token}' matches nobody")
        else:
            unresolved.append(f"'{token}' matches {', '.join(matches)}")
    return expected, unresolved


def load_tests(engine, root, people, problems):
    """[(filename, [Face], [expected people])] for the loose top-level photos.

    Every face is kept. This is the scan path, not enrolment: a photo with
    four people should produce four tags, so nothing is filtered by dominance.
    """
    tests = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path) or not faces.is_supported(path):
            continue
        expected, unresolved = parse_expected(name, people)
        if unresolved:
            problems.append(f"test {name}: {'; '.join(unresolved)} — NOT SCORED")
            continue
        if not expected:
            problems.append(f"test {name}: filename names nobody — NOT SCORED")
            continue
        try:
            found = engine.faces_in_image(path)
        except ValueError as e:
            problems.append(f"test {name}: {e}")
            continue
        if not found:
            problems.append(f"test {name}: NO FACE DETECTED (expected "
                            f"{', '.join(expected)})")
        tests.append((name, found, expected))
    return tests


# --- matching ---

def score_faces(found, enrolled):
    """[{person: score}] — each face against each enrolled person.

    A person's score is their best reference, matching the plan's rule that
    1-5 references exist so that any one of them can be the one that matches.
    Computed once per photo and reused for every threshold.
    """
    rows = []
    for face in found:
        rows.append({
            person: max(faces.cosine_similarity(face.embedding, r.embedding)
                        for r in refs)
            for person, refs in enrolled.items() if refs
        })
    return rows


def tag(rows, threshold):
    """{person: (score, face_index)} — the tags this photo would get.

    Two rules from the plan, both enforced here:
      * one face maps to at most one person, so siblings cannot both be
        tagged onto the same face — the best-scoring person wins it;
      * FileTags is keyed (file_hash, person_id), so a person appears at most
        once per photo even if two faces match them.
    """
    tags = {}
    for idx, scores in enumerate(rows):
        if not scores:
            continue
        person = max(scores, key=scores.get)
        score = scores[person]
        if score < threshold:
            continue
        if person not in tags or score > tags[person][0]:
            tags[person] = (score, idx)
    return tags


def evaluate(tests, enrolled, thresholds):
    """Per-threshold tag tallies, plus genuine/impostor score distributions."""
    scored = [(name, score_faces(found, enrolled), expected)
              for name, found, expected in tests]

    genuine, impostor = [], []
    for _, rows, expected in scored:
        for person in enrolled:
            best = max((r.get(person, -1.0) for r in rows), default=-1.0)
            if best < -0.5:
                continue
            (genuine if person in expected else impostor).append(best)

    results = {}
    for t in thresholds:
        right = wrong = missed = 0
        detail = []
        for name, rows, expected in scored:
            tags = tag(rows, t)
            got = set(tags)
            right += len(got & set(expected))
            wrong += len(got - set(expected))
            missed += len(set(expected) - got)
            detail.append((name, expected, tags))
        total_expected = right + missed
        results[t] = {
            "right": right, "wrong": wrong, "missed": missed,
            "recall": right / total_expected if total_expected else 0.0,
            "precision": right / (right + wrong) if right + wrong else 1.0,
            "detail": detail,
        }
    return results, genuine, impostor, scored


def pick_threshold(results, thresholds):
    """Lowest threshold with no wrong tags; else the one with fewest wrongs.

    Biased against wrong tags on purpose: a missed photo is invisible, a
    wrongly tagged one turns up in another person's search results.
    """
    clean = [t for t in thresholds if results[t]["wrong"] == 0]
    if clean:
        return max(clean, key=lambda t: (results[t]["right"], -t)), True
    return min(thresholds, key=lambda t: (results[t]["wrong"], -results[t]["right"])), False


# --- reporting ---

def _stats(scores):
    if not scores:
        return "(none)"
    a = np.array(scores)
    return (f"n={len(a):<4} min={a.min():.3f}  median={np.median(a):.3f}  "
            f"max={a.max():.3f}")


def report(model_id, refs, tests, problems, timings, ref_sweep_trials):
    print(f"\n{'=' * 78}\n  {model_id}\n{'=' * 78}")
    enrolled = {p: [f for _, f in entries] for p, entries in refs.items()}

    print(f"\n[1] ENROLLED  {len(refs)} people")
    for person, entries in sorted(refs.items()):
        print(f"      {person:<14} {len(entries)} reference photos")

    n_faces = sum(len(f) for _, f, _ in tests)
    print(f"\n[2] TEST PHOTOS  {len(tests)} photos, {n_faces} faces detected")
    for name, found, expected in tests:
        flag = "" if len(found) == len(expected) else \
               f"   <- {len(found)} faces but {len(expected)} expected"
        print(f"      {name:<22} {len(found)} face(s)  expect: "
              f"{', '.join(expected)}{flag}")

    if problems:
        print(f"\n    problems ({len(problems)}):")
        for p in problems[:20]:
            print(f"      {p}")
        if len(problems) > 20:
            print(f"      ... and {len(problems) - 20} more")

    if not tests:
        print("\n    no scorable test photos — nothing to measure")
        return None

    results, genuine, impostor, scored = evaluate(tests, enrolled, THRESHOLDS)

    print("\n[3] SEPARATION  (each photo's best score per person)")
    print(f"      genuine  (person IS in the photo)   {_stats(genuine)}")
    print(f"      impostor (person is NOT)            {_stats(impostor)}")
    if genuine and impostor:
        gap = min(genuine) - max(impostor)
        print(f"      gap {gap:+.3f}   " + ("cleanly separable — any threshold in "
              f"between works" if gap > 0 else "OVERLAP — no threshold is perfect"))

    print("\n[4] THRESHOLD SWEEP   (tags across all test photos)")
    print("      thresh   right   wrong   missed   precision   recall")
    for t in REPORT_AT:
        r = results[min(THRESHOLDS, key=lambda x: abs(x - t))]
        flag = "  <- configured" if abs(t - faces.COSINE_THRESHOLD) < 1e-6 else ""
        print(f"      {t:.3f}  {r['right']:>6}  {r['wrong']:>6}  {r['missed']:>7}"
              f"      {r['precision']:6.1%}   {r['recall']:6.1%}{flag}")

    best_t, clean = pick_threshold(results, THRESHOLDS)
    r = results[best_t]
    print(f"\n      -> recommended {best_t:.3f}: {r['right']} right, {r['wrong']} wrong, "
          f"{r['missed']} missed"
          + ("" if clean else "   (no threshold avoids wrong tags)"))

    print(f"\n[5] PER-PHOTO RESULT at {best_t:.3f}")
    for name, expected, tags in r["detail"]:
        got = set(tags)
        exp = set(expected)
        if got == exp:
            mark, note = "ok  ", ""
        else:
            mark = "FAIL"
            bits = []
            if exp - got:
                bits.append("missed " + ", ".join(sorted(exp - got)))
            if got - exp:
                bits.append("WRONGLY tagged " + ", ".join(sorted(got - exp)))
            note = "   " + "; ".join(bits)
        shown = ", ".join(f"{p} {tags[p][0]:.3f}" for p in sorted(tags)) or "nobody"
        print(f"      {mark} {name:<22} -> {shown}{note}")

    print("\n[6] HOW MANY REFERENCE PHOTOS ARE NEEDED")
    print(f"      (random subsets of each person's references, "
          f"{ref_sweep_trials} draws averaged)")
    print("      refs   right   wrong   missed   at threshold")
    max_refs = max(len(e) for e in refs.values())
    rng = random.Random(0)
    for n in range(1, max_refs + 1):
        acc = {"right": 0, "wrong": 0, "missed": 0}
        runs = 0
        for _ in range(ref_sweep_trials):
            subset = {p: rng.sample(v, min(n, len(v))) for p, v in enrolled.items()}
            sub_results, _, _, _ = evaluate(tests, subset, [best_t])
            for k in acc:
                acc[k] += sub_results[best_t][k]
            runs += 1
            if all(len(v) <= n for v in enrolled.values()):
                break          # every person exhausted: subsets are deterministic
        print(f"      {n:>4}   {acc['right'] / runs:5.1f}   {acc['wrong'] / runs:5.1f}"
              f"   {acc['missed'] / runs:6.1f}   {best_t:.3f}")

    if timings:
        per_photo = np.median(timings["photo_ms"])
        print("\n[7] SPEED  (warmed up)")
        print(f"      {per_photo:7.1f} ms per photo   "
              f"({np.median(timings['face_ms']):.1f} ms per face)")
        print(f"      {1000 / per_photo:7.1f} photos/sec  ->  20,000 photos in "
              f"{20000 * per_photo / 1000 / 60:.0f} min")

    return {"model_id": model_id, "results": results, "best_t": best_t,
            "clean": clean, "genuine": genuine, "impostor": impostor,
            "timings": timings}


def benchmark(engine, image_paths, runs=3):
    images = [faces.read_image(p) for p in image_paths]
    images = [im for im in images if im is not None]
    if not images:
        return None
    engine.faces_in_array(images[0])
    photo_ms, face_ms = [], []
    for _ in range(runs):
        for im in images:
            t0 = time.perf_counter()
            found = engine.faces_in_array(im)
            dt = (time.perf_counter() - t0) * 1000
            photo_ms.append(dt)
            if found:
                face_ms.append(dt / len(found))
    return {"photo_ms": photo_ms, "face_ms": face_ms or [0]}


def single_image(model_ids, path):
    """Similarity matrix of the faces inside one photo."""
    for model_id in model_ids:
        engine = faces.FaceEngine(model_id)
        found = sorted(engine.faces_in_image(path), key=lambda f: f.x)
        print(f"\n{'=' * 78}\n  {model_id}  —  {os.path.basename(path)}\n{'=' * 78}")
        print(f"\n  {len(found)} faces (left to right):")
        for i, f in enumerate(found):
            print(f"    [{i}] {f.w}x{f.h}px at ({f.x},{f.y})  det_score={f.det_score:.3f}")
        if len(found) < 2:
            continue
        print("\n  cosine similarity:")
        print("        " + "".join(f"{i:>8}" for i in range(len(found))))
        for i, a in enumerate(found):
            cells = "".join(f"{faces.cosine_similarity(a.embedding, b.embedding):>8.3f}"
                            for b in found)
            print(f"    [{i}]" + cells)
        off = [faces.cosine_similarity(found[i].embedding, found[j].embedding)
               for i in range(len(found)) for j in range(i + 1, len(found))]
        print(f"\n  highest between two different faces: {max(off):.3f}  "
              f"(threshold {faces.COSINE_THRESHOLD})")


def verdict(results):
    print(f"\n{'=' * 78}\n  VERDICT\n{'=' * 78}\n")
    print(f"  {'model':<10}{'threshold':>11}{'right':>8}{'wrong':>8}{'missed':>8}"
          f"{'recall':>9}{'ms/photo':>11}")
    for r in results:
        if not r:
            continue
        name = r["model_id"].rsplit("_", 1)[1]
        v = r["results"][r["best_t"]]
        ms = np.median(r["timings"]["photo_ms"]) if r["timings"] else float("nan")
        print(f"  {name:<10}{r['best_t']:>11.3f}{v['right']:>8}{v['wrong']:>8}"
              f"{v['missed']:>8}{v['recall']:>9.1%}{ms:>11.1f}")
    print("\n  wrong  : tagged as someone who is NOT in the photo — the costly error,")
    print("           it puts a photo into another person's search results")
    print("  missed : person in the photo but not tagged — the photo is simply absent")
    print("  recall : share of the people present who were correctly tagged")
    print("\n  Choose on wrong first, then recall, then speed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", help="the people/ folder, or a single photo")
    parser.add_argument("--models", default="int8,fp32",
                        help="comma-separated variants to compare (default: int8,fp32)")
    parser.add_argument("--dominance", type=float, default=DOMINANCE_RATIO,
                        help="a reference photo's largest face counts as the subject "
                             f"only if the next is smaller than this fraction of it "
                             f"(default: {DOMINANCE_RATIO})")
    parser.add_argument("--force-largest", action="store_true",
                        help="use a reference photo's largest face even when it is not "
                             "dominant (risks enrolling the wrong person)")
    parser.add_argument("--det-threshold", type=float, default=None,
                        help="detector confidence floor; lower finds more faces but "
                             f"risks spurious ones (default: {faces.DET_SCORE_THRESHOLD})")
    parser.add_argument("--trials", type=int, default=10,
                        help="random draws when sweeping reference count (default: 10)")
    args = parser.parse_args()

    model_ids = [f"yunet2023mar_sface2021dec_{m.strip()}" for m in args.models.split(",")]
    for model_id in model_ids:
        if model_id not in faces.MODELS:
            sys.exit(f"unknown model: {model_id}")
        if faces.missing_models(model_id):
            sys.exit(f"{model_id} has missing files. Run: "
                     f"python tools/fetch_models.py --all")

    if os.path.isfile(args.path):
        single_image(model_ids, args.path)
        return 0
    if not os.path.isdir(args.path):
        sys.exit(f"not found: {args.path}")

    out = []
    for model_id in model_ids:
        engine = faces.FaceEngine(model_id, args.det_threshold)
        problems = []
        refs = load_references(engine, args.path, args.dominance,
                               args.force_largest, problems)
        if not refs:
            sys.exit(f"no per-person folders with usable photos under {args.path}")
        tests = load_tests(engine, args.path, list(refs), problems)
        paths = [os.path.join(args.path, n) for n, _, _ in tests]
        out.append(report(model_id, refs, tests, problems,
                          benchmark(engine, paths), args.trials))

    if len([r for r in out if r]) > 1:
        verdict(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
