"""Helpers for ansible.builtin.file-compatible state inference."""


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
