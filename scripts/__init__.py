"""Repository tooling that is not part of the distribution.

Nothing here is packaged -- the wheel contains `src/anidb_client` only. This is a
package rather than a bare directory so the modules can be imported by the test
suite, which is what keeps them from being untested shell-adjacent glue.
"""
