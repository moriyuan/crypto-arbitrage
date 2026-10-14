from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from build_tape import HEADER, UPDATE, MAGIC, ObservedDepth, exact_int

def books(askqty=80, second=True):
    rows = ((D('1.0'), D(askqty)),) + (((D('1.1'), D(20)),) if second else ())
    return {v: SimpleNamespace(asks=rows, bids=((D('.9'), D(40)),)) for v in ('bybit','bitget')}

def test_wire_format_and_integer_validation():
    assert len(MAGIC) == 8 and HEADER.size == 80 and UPDATE.size == 24
    assert HEADER.unpack(HEADER.pack(123,1,2,1,0,1,*range(8)))[-1] == 7
    assert exact_int('10', 'qty') == 10
    with pytest.raises(ValueError): exact_int('10.5', 'qty')

def test_unchanged_observation_cannot_replenish_candidate_budget():
    o = ObservedDepth()
    first = books()
    assert len(o.encode(first)[2]) == 6
    assert o.encode(first)[2] == []
    assert o.encode(books())[2] == []
    assert sorted(o.encode(books(askqty=100))[2]) == [(0,100000000,100),(2,100000000,100)]

def test_leaving_top50_deletes_memory_and_returning_is_new():
    o = ObservedDepth(); o.encode(books())
    assert sorted(o.encode(books(second=False))[2]) == [(0,110000000,0),(2,110000000,0)]
    assert sorted(o.encode(books())[2]) == [(0,110000000,20),(2,110000000,20)]

def test_off_tick_far_level_is_preserved_without_rounding():
    o = ObservedDepth(); b = books()
    b['bybit'].asks = ((D('1'),D(80)), (D('1.492324'),D(15)))
    prices, quantities, updates = o.encode(b)
    assert prices[0] == 100000
    assert (0,149232400,15) in updates
    b['bybit'].asks = ((D('1'),D(80)), (D('1.492325'),D(15)))
    updates = o.encode(b)[2]
    assert (0,149232400,0) in updates and (0,149232500,15) in updates
