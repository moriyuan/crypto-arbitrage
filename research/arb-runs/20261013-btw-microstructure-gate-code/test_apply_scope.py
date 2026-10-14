import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('microstructure_scope', Path(__file__).with_name('apply_scope.py'))
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def test_all_persistence_gates_removed_without_name_heuristic_or_mutation():
    rows = [dict(id='baseline', control_id='baseline', min_spread_persistence_ms=0),
            dict(id='coverage', control_id='coverage', min_spread_persistence_ms=0),
            dict(id='book', control_id='coverage', min_spread_persistence_ms=0),
            dict(id='unrelated_name', control_id='coverage', min_spread_persistence_ms=50),
            dict(id='combined', control_id='coverage', min_spread_persistence_ms=100)]
    before = copy.deepcopy(rows)
    active, excluded = M.partition(rows)
    assert [p['id'] for p in active] == ['baseline', 'coverage', 'book']
    assert [p['id'] for p in excluded] == ['unrelated_name', 'combined']
    assert rows == before


def test_filtering_cannot_silently_drop_required_control():
    rows = [dict(id='baseline', control_id='baseline', min_spread_persistence_ms=0),
            dict(id='book', control_id='removed', min_spread_persistence_ms=0)]
    with pytest.raises(ValueError, match='missing control'):
        M.partition(rows)


@pytest.mark.parametrize('duration', [-1, '0', float('nan')])
def test_bad_duration_rejected(duration):
    # NaN must fail rather than be silently treated as a persistence policy.
    with pytest.raises(ValueError):
        M.partition([dict(id='baseline', control_id='baseline', min_spread_persistence_ms=duration)])
