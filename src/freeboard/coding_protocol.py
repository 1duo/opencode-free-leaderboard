"""Frozen coding extensions; changes require a new panel identifier."""

SPECS = {
    'bigcodebench-cpu-v1': {
        'benchmark': 'bigcodebench', 'strata': {'stdlib': 12, 'scientific': 12},
        'label': 'practical Python',
    },
    'systems-c-v1': {
        'benchmark': 'systems_c',
        'strata': {'memory': 6, 'encoding': 6, 'parsing': 6, 'numeric': 6},
        'label': 'systems C',
    },
    'humanevalpack-multilingual-v1': {
        'benchmark': 'humanevalpack',
        'strata': {f'{language}_{task}': 4 for language in ['cpp', 'go', 'rust']
                   for task in ['generation', 'repair']},
        'label': 'C++ / Rust / Go',
    },
}


def coding_spec(manifest: dict) -> dict:
    try:
        return SPECS[manifest['protocol']['panel']]
    except KeyError:
        raise ValueError('Unknown frozen coding protocol') from None
