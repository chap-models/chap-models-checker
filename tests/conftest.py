"""pytest config root for chap-models-checker.

Keeps pytest off chap-core artifact directories it occasionally drops
into the working dir (`runs/`, etc.). The checker doesn't ship tests
yet — this directory exists so `pytest` exits with `no tests ran` cleanly
instead of crawling sibling repos.
"""
