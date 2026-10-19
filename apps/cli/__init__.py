# The CLI's WebRTC stack is an optional extra (docs/decisions.md D31);
# fail with instructions rather than an ImportError traceback.
try:
    import aiortc  # noqa: F401
    import websockets  # noqa: F401
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        f"whoosh: missing dependency '{exc.name}'. Install the CLI with its extra:\n"
        '    uv tool install "whoosh[cli] @ git+https://github.com/sahil3105kumar/Whoosh"'
    ) from None
