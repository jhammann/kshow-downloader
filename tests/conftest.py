import warnings

# These libraries warn at import time that they prefer httpx2. pyproject's filterwarnings
# ignores that, but a command-line `-W error` overrides it, so import them once here with
# the warnings off.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    warnings.filterwarnings("ignore", message="The httpx module is deprecated")
    import authlib.integrations.starlette_client  # noqa: F401
    import starlette.testclient  # noqa: F401
