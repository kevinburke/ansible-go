"""Helpers for ansible.builtin.file-compatible state inference."""


def format_octal_mode(mode):
    """Return a zero-prefixed octal mode, or None for unsupported modes."""
    if mode is None:
        return None
    if isinstance(mode, int):
        return f"0{mode:o}"

    mode_str = str(mode)
    try:
        int(mode_str, 8)
    except ValueError:
        return None

    if not mode_str.startswith("0"):
        mode_str = "0" + mode_str
    return mode_str


def requires_builtin_file(args):
    """Return True when the file fast path would not match ansible-core."""
    state = args.get("state")
    src = args.get("src")

    if format_octal_mode(args.get("mode")) is None and args.get("mode") is not None:
        return True

    if (
        args.get("modification_time") is not None
        or args.get("access_time") is not None
        or args.get("modification_time_format") is not None
        or args.get("access_time_format") is not None
    ):
        return True

    if state in ("link", "hard") or (state is None and src):
        return True

    if args.get("follow") is not None and not args.get("follow"):
        return state != "absent"

    return False


def infer_file_state(client, path, state, src, recurse, follow):
    if state is not None:
        return state
    if src:
        return "link"
    if recurse:
        return "directory"

    stat_result = client.stat(path, follow=follow, checksum=False)
    if stat_result.get("exists", False) and stat_result.get("isdir", False):
        return "directory"
    return "file"
