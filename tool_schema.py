"""Short wire descriptions; validation schemas stay intact."""
import copy


DESCRIPTIONS = {
    'list_files':'List tree; follow continuation_paths.',
    'read_file':'Read UTF-8 lines (1-based, inclusive).',
    'search_text':'Find literal text; paginate with next_cursor.',
    'search_regex':'Find regex per line; paginate with next_cursor.',
    'create_file':'Create UTF-8 file; must not exist.',
    'replace_text':'Replace one exact unique UTF-8 fragment.',
    'run_check':'Run a named workspace check.',
    'apply_patch':'Apply unified diff; no deletes or renames.',
    'git_status':'Read Git status.', 'git_diff':'Read tracked diff.',
    'git_log':'Read commit history.', 'git_show':'Read a commit.',
    'git_create_branch':'Create and switch to a new branch; call alone.',
    'prepare_commit':'Inspect changes and get commit fingerprint.',
    'create_commit':'Commit prepared files; requires checks; call alone.',
}

FIELDS = {
    'path':'Workspace-relative path.',
    'file_pattern':'Case-sensitive glob; ** crosses directories.',
    'cursor':'Previous next_cursor; same query.',
    'expected_sha256':'read_file content_sha256.',
    'interface_notes':'Changed public interfaces only.',
    'old_text':'Exact unique fragment.',
    'new_text':'Replacement; empty deletes fragment.',
    'note':'Interface meaning.',
    'symbol':'Declared symbol.',
}

DEFAULTS = {
    'list_files':{'path':'Relative directory; . is root.', 'max_depth':'Default 3.', 'max_entries':'Default 400.'},
    'read_file':{'start_line':'Default 1.', 'end_line':'Inclusive; default start+199.'},
    'search_text':{'path':'Relative path; default .', 'case_sensitive':'Default false.', 'context_lines':'Default 0.', 'max_results':'Default 100.'},
    'search_regex':{'path':'Relative path; default .', 'case_sensitive':'Default false.', 'context_lines':'Default 0.', 'max_results':'Default 100.'},
}


def compact_tools(tools):
    exposed = copy.deepcopy(tools)

    def shorten(node, name):
        if not isinstance(node, dict):
            return
        node.pop('description', None)
        for field, child in node.get('properties', {}).items():
            shorten(child, name)
            hint = DEFAULTS.get(name, {}).get(field, FIELDS.get(field))
            if hint:
                child['description'] = hint
        if 'items' in node:
            shorten(node['items'], name)

    for tool in exposed:
        function = tool['function']
        name = function['name']
        function['description'] = DESCRIPTIONS.get(name, function.get('description',''))
        shorten(function['parameters'], name)
    return exposed
