"""Causal, equally spaced short-horizon variance for an AS quote unit.

The caller supplies authoritative grid book-validity and connection segment IDs.
No forward fill through invalid/stale quotes or connection changes is permitted.
This module never infers raw book freshness from a previous tape validity flag.
"""
from collections import deque
from decimal import Decimal, localcontext

NS=1_000_000_000

class RollingSigma:
    def __init__(self,window_seconds=60,tick_size='0.00001',quote_unit_coins='10'):
        if not isinstance(window_seconds,int) or window_seconds<1:raise ValueError('window must be positive integer seconds')
        self.window=window_seconds;self.tick=Decimal(tick_size);self.unit=Decimal(quote_unit_coins)
        if not self.tick.is_finite() or self.tick<=0 or not self.unit.is_finite() or self.unit<=0:raise ValueError('positive finite units required')
        self.samples=deque();self.squared=deque();self.sum_squared=Decimal(0)
        self.segment=None;self.last_seen=None;self.resets=0;self.last_reason='warming_up'

    def clear(self,reason):
        self.samples.clear();self.squared.clear();self.sum_squared=Decimal(0)
        self.last_reason=reason;self.resets+=1

    def add(self,second_ns,mid_ticks_twice,valid,segment):
        if not isinstance(second_ns,int) or second_ns%NS:raise ValueError('observations must lie on exact integer-second grid')
        if self.last_seen is not None and second_ns<=self.last_seen:raise ValueError('non-increasing sample time')
        consecutive=self.last_seen is None or second_ns-self.last_seen==NS
        self.last_seen=second_ns
        if not valid:
            self.clear('invalid_or_stale_book');self.segment=segment;return self.result()
        if not isinstance(mid_ticks_twice,int) or mid_ticks_twice<=0:raise ValueError('valid observation needs positive integer twice-mid ticks')
        if not consecutive:self.clear('missing_grid_observation')
        if self.segment is not None and segment!=self.segment:self.clear('connection_segment_changed')
        self.segment=segment
        with localcontext() as ctx:
            ctx.prec=50
            value=Decimal(mid_ticks_twice)*self.tick*self.unit/2
            if self.samples:
                ds=value-self.samples[-1][1];sq=ds*ds;self.squared.append(sq);self.sum_squared+=sq
            self.samples.append((second_ns,value))
            if len(self.samples)>self.window+1:
                self.samples.popleft();self.sum_squared-=self.squared.popleft()
        self.last_reason='ready' if len(self.squared)==self.window else 'warming_up'
        return self.result()

    def result(self):
        ready=len(self.squared)==self.window
        return {'ready':ready,'reason':self.last_reason,'window_seconds':self.window,
          'samples':len(self.samples),'increments':len(self.squared),'segment':self.segment,
          'known_through_ns':self.samples[-1][0] if self.samples else None,
          'window_start_ns':self.samples[0][0] if self.samples else None,
          'sigma2_usdt2_per_second':str(self.sum_squared/self.window) if ready else None,
          'quote_unit_coins':str(self.unit),'tick_size':str(self.tick),'reset_count':self.resets,
          'estimator':'sum consecutive 1s squared absolute quote-unit mid increments / elapsed seconds; no demeaning'}
