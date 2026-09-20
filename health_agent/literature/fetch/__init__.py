"""The corpus's network code, and nothing else's.

Two commands import this package: `literature build-pack` (NCBI E-utilities,
maintainer) and `literature install` (a GitHub Release asset, user). Nothing
on the ask path does, and `tests/test_no_network.py` proves it statically.
`client.py` is the only file here, or anywhere under `literature/`, that
imports a transport.
"""
