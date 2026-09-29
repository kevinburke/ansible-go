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

    # Only ansible's time keywords run on the fast path. "preserve" is the
    # default for every state except touch, and the agent never changes
    # timestamps for those states; touch applies "now" and "preserve" in the
    # agent. Explicit timestamps (which are what *_time_format parses) run
    # stock. The formats are ignored for the keywords, as stock ignores them.
    for key in ("modification_time", "access_time"):
        value = args.get(key)
        if value is None or value == "preserve":
            continue
        if value == "now" and state == "touch":
            continue
        return True

    if state in ("link", "hard") or (state is None and src):
        return True

    if args.get("follow") is not None and not args.get("follow"):
        return state != "absent"

    return False


def touch_preserves_times(args):
    """Return True when a state=touch task keeps both existing timestamps."""
    return (
        args.get("modification_time") == "preserve"
        and args.get("access_time") == "preserve"
    )


def attributes_differ(stat_result, owner, group, mode):
    """Return True when owner, group or mode would change an existing path.

    owner and group may be names or numeric ids, as ansible.builtin.file
    accepts either. mode must already be a format_octal_mode() string.
    """
    if mode is not None and int(mode, 8) != int(stat_result.get("mode") or "0", 8):
        return True
    for want, name_key, id_key in ((owner, "owner", "uid"), (group, "group", "gid")):
        if want is None:
            continue
        want = str(want)
        if want != stat_result.get(name_key) and want != str(stat_result.get(id_key)):
            return True
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
