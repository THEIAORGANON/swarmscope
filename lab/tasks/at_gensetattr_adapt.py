"""at-gensetattr hidden tests: the fix's tests/, with TestLazyIsGenerator (it imports the new private
_lazy_is_generator / _IS_GENERATOR_RESULTS and checks their cache) replaced by a public check of its one public
consequence: an unhashable callable works as an on_setattr hook (ChatGPT review 21.11: no new private names as a
hidden contract). usage: adapt_at.py FIX_TESTS_DIR OUT_TESTS_DIR"""
import shutil, sys
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[2])
shutil.rmtree(out, ignore_errors=True)
shutil.copytree(src, out)
f = out / "test_compat.py"
s = f.read_text()
imp = "from attr._compat import _IS_GENERATOR_RESULTS, _lazy_is_generator\n"
assert s.count(imp) == 1
s = s.replace(imp, "")
start = s.index("class TestLazyIsGenerator:")
end = s.index("\nclass ", start + 1) if "\nclass " in s[start + 1:] else len(s)
s = s[:start] + '''class TestUnhashableHook:
    def test_unhashable_callable_hook(self):
        """
        An on_setattr hook that cannot be hashed still works (public consequence
        of the upstream TestLazyIsGenerator.test_not_hashable).
        """
        import attr

        class Hook:
            __hash__ = None

            def __call__(self, instance, attrib, value):
                return value * 2

        @attr.s
        class C:
            x = attr.ib(on_setattr=Hook())

        c = C(1)
        c.x = 2

        assert 4 == c.x
''' + s[end:]
assert "_lazy_is_generator" not in s and "_IS_GENERATOR_RESULTS" not in s
f.write_text(s)
for p in out.rglob("*.py"):
    assert "_lazy_is_generator" not in p.read_text(), p
print("adapted")
