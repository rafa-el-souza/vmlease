"""Local type stub for the `google-re2` binding — ONLY the surface vmlease uses.

`google-re2` ships no type information, so we declare the minimal accurate shape
of the calls in `vmlease.assertions`: `re2.compile(pattern, options=...)`, an
`Options` carrying `max_mem`, the compiled regexp's `.search`, and the
re-compatible `error` compile-failure type. This is a stub on `mypy_path`, not a
runtime override; the real C++ binding is what executes.
"""

class Options:
    max_mem: int
    def __init__(self) -> None: ...

class error(Exception): ...

class _Regexp:
    def search(self, text: str) -> object | None: ...

def compile(pattern: str, options: Options | None = ...) -> _Regexp: ...
