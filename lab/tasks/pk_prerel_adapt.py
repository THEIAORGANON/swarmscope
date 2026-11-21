"""pk-prerel hidden tests: the fix's tests/, with every read of the new private ``_pre_region`` replaced by public
behaviour (ChatGPT review, 21.11). Rule: keep each test's public checks; drop a ``_pre_region`` assert where the
test's own filter() already shows the same thing; otherwise check the effect through filter() on an input holding a
pre-release AND a final release in range (contains() ignores the autodetected opt-in, so it cannot show it).
One purely structural property test (test_region_stays_within_bounds) is deselected in the test command instead.
usage: adapt.py FIX_TESTS_DIR OUT_TESTS_DIR"""
import shutil
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
shutil.rmtree(out, ignore_errors=True)
shutil.copytree(src, out)
f = out / "test_ranges.py"
s = f.read_text()

# (old, new): each old must occur exactly once
EDITS = [
    # test_prerelease_autodetect: the opt-in admits an in-range pre-release next to a final; none without one
    ('''        assert vr(">=1.0a1")._pre_region == vr(">=1.0a1")._bounds
        assert vr(">=1.0a1")._pre_region
        assert vr(">=1.0")._pre_region == ()
''', '''        assert list(vr(">=1.0a1").filter(["1.5a1", "1.5"])) == ["1.5a1", "1.5"]
        assert list(vr(">=1.0").filter(["1.5a1", "1.5"])) == ["1.5"]
'''),
    # test_union_full_preserves_autodetected_prerelease_policy: region = r's bounds, in both orders
    ('''        assert (r | VersionRange.full())._pre_region == r._bounds
        assert (VersionRange.full() | r)._pre_region == r._bounds
''', '''        for u in (r | VersionRange.full(), VersionRange.full() | r):
            assert list(u.filter(["1.5a1", "1.5"])) == ["1.5a1", "1.5"]
            assert list(u.filter(["0.5a1", "0.5"])) == ["0.5"]
'''),
    # test_union_keeps_opt_in_scoped_to_originating_range: its filter() already shows the scope
    ('''        assert u._pre_region == vr(">=2.0b1")._bounds
''', ''),
    # test_narrowing_permanently_sheds_out_of_bounds_opt_in: its filter() calls show the clip
    ('''        assert inter._pre_region == inter._bounds  # clipped to [1.0a1, 1.5)
''', ''),
    # test_full_intersection_keeps_operand_opt_in: its filter() shows the kept opt-in; add the region's lower edge
    ('''        assert (VersionRange.full() & req)._pre_region == req._bounds
''', '''        assert list((VersionRange.full() & req).filter(["1.5b1", "1.5", "2.5"])) == ["2.5"]
'''),
    # test_double_complement_drops_region_keeps_versions: its filter() shows the drop
    ('''        assert (~~r)._pre_region == ()
        cand''', '''        cand'''),
    # test_difference_minuend_with_configured_policy: its filter() shows no opt-in
    ('''        assert d._pre_region == ()
        assert list(d.filter(["1.5b1", "1.0"])) == ["1.0"]''', '''        assert list(d.filter(["1.5b1", "1.0"])) == ["1.0"]'''),
    # test_difference_equals_intersect_complement_when_b_complement_derived: == and filter() cover it
    ('''        assert left._pre_region == right._pre_region
''', ''),
    # test_construction_region_matches_algebra: same opt-in -> same filter() on pre-releases and finals
    ('''        assert direct._pre_region == composed._pre_region
        assert direct == composed
        pool = ["1.0a1", "2.0b1", "2.9", "3.0b1", "4.5"]''', '''        assert direct == composed
        pool = ["1.0a1", "2.0b1", "2.9", "3.0b1", "4.5"]
        assert list(direct.filter(pool)) == list(composed.filter(pool)) == ["1.0a1", "2.0b1", "2.9"]'''),
    ('''        assert direct_arb._pre_region == composed_arb._pre_region
''', '''        arb_pool = ["2.0", "1.5a1", "1.5", "2.0b1"]
        assert list(direct_arb.filter(arb_pool)) == list(composed_arb.filter(arb_pool))
'''),
    # test_empty_bounds_intersection_equals_empty: == empty() and the re-widened filter() cover it
    ('''        assert empty_region._pre_region == ()
''', ''),
    # test_disjoint_region_does_not_force_admit_gap: its filter() shows both region parts and the gap
    ('''        assert len(r._pre_region) == 2
''', ''),
    # test_difference_keeps_minuend_region_clipped: the clip shows after re-widening past 5.0
    ('''        assert d._pre_region == d._bounds  # clipped to [2.0b1, 5.0)
''', '''        assert list((d | vr(">=5.0")).filter(["6.0b1", "6.0"])) == ["6.0"]
'''),
    # test_difference_by_empty_bounds_range: the equalities and filter() cover it
    ('''        assert b._pre_region == ()
''', ''),
    # test_configured_policy_with_prerelease_spec_mirrors_specifier_set: filter()/contains() mirror the set
    ('''            assert r._pre_region == ()
''', ''),
    # test_double_complement_is_erasure_not_identity
    ('''        assert (~~b)._pre_region == ()
        assert b._pre_region
''', '''        assert list((~~b).filter(["2.0b1", "2.5"])) == ["2.5"]
        assert list(b.filter(["2.0b1", "2.5"])) == ["2.0b1", "2.5"]
'''),
    # test_double_difference_sheds_opt_in
    ('''        assert (a - (a - b))._pre_region == ()
        assert (a & b)._pre_region
''', '''        assert list((a - (a - b)).filter(["2.0b1", "2.5"])) == ["2.5"]
        assert list((a & b).filter(["2.0b1", "2.5"])) == ["2.0b1", "2.5"]
'''),
    # test_floor_exclusive_upper_is_empty: == empty() covers it
    ('''        assert r == VersionRange.empty()
        assert r._pre_region == ()
''', '''        assert r == VersionRange.empty()
'''),
    # test_exact_equals_successor_interval_bounds: no pre-release lies in [1.0, 1.0.post0.dev0), so the regions
    # differ only structurally; != (structural equality, documented) is the public check
    ('''        assert a._pre_region == ()
        assert b._pre_region != ()
''', ''),
]
for old, new in EDITS:
    n = s.count(old)
    assert n == 1, (n, old[:70])
    s = s.replace(old, new)
assert "_pre_region" not in s.replace("``_pre_region``", ""), "a read of _pre_region is left"
# the class docstring may still name it in prose: harmless
f.write_text(s)
for p in out.rglob("*.py"):
    t = p.read_text()
    for line in t.splitlines():
        if "_pre_region" in line and not line.strip().startswith(("#", '"', "``")) and "``_pre_region``" not in line:
            if p.name != "test_ranges_set_algebra.py" or "r._pre_region" not in line:
                raise SystemExit(f"left in {p}: {line.strip()}")
print("adapted:", len(EDITS), "edits")
