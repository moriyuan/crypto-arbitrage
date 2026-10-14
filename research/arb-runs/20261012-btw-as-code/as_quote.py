"""Finite-horizon Avellaneda-Stoikov first-order quote approximation.

Primary equation reference: Avellaneda & Stoikov, Quantitative Finance 8(3),
2008, equations (29)-(30), https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf
Monetary and inventory units are explicit: one inventory unit is a fixed lot
of quote_unit_coins, S is USDT/lot, sigma² is USDT²/lot²/second, gamma and k are
inverse USDT for that quote unit. A estimates arrival intensity but cancels
from this first-order quote formula. This is not a queue or execution model.
"""
from dataclasses import dataclass
from decimal import Decimal as D,InvalidOperation,localcontext,ROUND_FLOOR,ROUND_CEILING
import re

class CalibrationError(ValueError):pass

def decimal(value,name,minimum=None,strict=False):
    try:n=D(str(value))
    except (InvalidOperation,ValueError):raise ValueError(f'{name} must be a finite decimal') from None
    if not n.is_finite() or (minimum is not None and (n<=minimum if strict else n<minimum)):
        raise ValueError(f'{name} outside allowed range')
    return n

def integer(value,name):
    if isinstance(value,bool) or not isinstance(value,int):raise ValueError(f'{name} must be an integer')
    return value

@dataclass(frozen=True)
class Calibration:
    symbol:str
    venue:str
    quote_unit_coins:D
    trained_from_ns:int
    trained_through_ns:int
    valid_from_ns:int
    valid_until_ns:int
    accepted:bool
    a_per_second:D
    k_per_usdt:D
    source_sha256:tuple
    fit_diagnostics:dict

    @classmethod
    def from_mapping(cls,m):
        try:
            if m['schema_version']!=1:raise ValueError('unsupported calibration schema')
            if not isinstance(m['accepted'],bool):raise ValueError('accepted must be boolean')
            names=('trained_from_ns','trained_through_ns','valid_from_ns','valid_until_ns')
            start,end,vfrom,vuntil=[integer(m[k],k) for k in names]
            if not 0<=start<=end<vfrom<vuntil:raise ValueError('invalid training / evaluation interval')
            hashes=m['source_sha256']
            if not isinstance(hashes,list) or not hashes or not all(isinstance(h,str) and re.fullmatch('[0-9a-f]{64}',h) for h in hashes):raise ValueError('source hashes missing or invalid')
            if m['venue'] not in ('bybit','bitget') or not isinstance(m['symbol'],str) or not m['symbol']:raise ValueError('invalid venue or symbol')
            return cls(m['symbol'],m['venue'],decimal(m['quote_unit_coins'],'quote_unit_coins',D(0),True),
              start,end,vfrom,vuntil,m['accepted'],decimal(m['a_per_second'],'a_per_second',D(0),True),
              decimal(m['k_per_usdt'],'k_per_usdt',D(0),True),tuple(hashes),dict(m['fit_diagnostics']))
        except (KeyError,TypeError,ValueError) as e:raise CalibrationError(str(e)) from e

    def require(self,symbol,venue,signal_ns):
        if not self.accepted:raise CalibrationError('calibration fit not accepted')
        if symbol!=self.symbol or venue!=self.venue:raise CalibrationError('calibration instrument or venue mismatch')
        if not self.trained_through_ns<signal_ns or not self.valid_from_ns<=signal_ns<self.valid_until_ns:raise CalibrationError('calibration unavailable, future or expired')

@dataclass(frozen=True)
class QuoteParameters:
    symbol:str
    venue:str
    signal_ns:int
    bid_ticks:int
    ask_ticks:int
    tick_size:D
    native_tick_units:int
    signed_unhedged_coins:D
    gamma_per_usdt:D
    horizon_seconds:D
    sigma2_usdt2_per_second:D
    sigma_known_through_ns:int
    sigma_ready:bool

    @classmethod
    def from_mapping(cls,m):
        try:
            ns=integer(m['signal_ns'],'signal_ns');bid=integer(m['bid_ticks'],'bid_ticks');ask=integer(m['ask_ticks'],'ask_ticks')
            known=integer(m['sigma_known_through_ns'],'sigma_known_through_ns')
            if bid<=0 or ask<=bid:raise ValueError('a non-crossed, positive book is required')
            if m['sigma_ready'] is not True or known>ns:raise ValueError('volatility unavailable or future')
            return cls(m['symbol'],m['venue'],ns,bid,ask,decimal(m['tick_size'],'tick_size',D(0),True),integer(m.get('native_tick_units',1),'native_tick_units'),
              decimal(m['signed_unhedged_coins'],'signed_unhedged_coins'),decimal(m['gamma_per_usdt'],'gamma_per_usdt',D(0)),
              decimal(m['horizon_seconds'],'horizon_seconds',D(0)),decimal(m['sigma2_usdt2_per_second'],'sigma2_usdt2_per_second',D(0)),known,True)
        except KeyError as e:raise ValueError(f'missing quote parameter {e}') from e


def as_quotes(p:QuoteParameters,c:Calibration):
    c.require(p.symbol,p.venue,p.signal_ns)
    with localcontext() as ctx:
        ctx.prec=50
        if p.native_tick_units<1:raise ValueError('native_tick_units must be positive')
        unit=c.quote_unit_coins
        mid=(D(p.ask_ticks)+p.bid_ticks)*p.tick_size/2*unit
        q=p.signed_unhedged_coins/unit
        risk=p.gamma_per_usdt*p.sigma2_usdt2_per_second*p.horizon_seconds
        inventory_shift=q*risk;reservation=mid-inventory_shift
        arrival_term=((1+p.gamma_per_usdt/c.k_per_usdt).ln()/p.gamma_per_usdt) if p.gamma_per_usdt else 1/c.k_per_usdt
        half=risk/2+arrival_term
        buy=(reservation-half)/unit;sell=(reservation+half)/unit
        if buy<=0 or sell<=buy:raise ValueError('AS produces nonpositive or crossed quote')
        bid=int((buy/p.tick_size/p.native_tick_units).to_integral_value(rounding=ROUND_FLOOR))*p.native_tick_units
        ask=int((sell/p.tick_size/p.native_tick_units).to_integral_value(rounding=ROUND_CEILING))*p.native_tick_units
        if bid<=0 or ask<=bid:raise ValueError('AS tick rounding produces invalid quote')
        return {'model':'avellaneda_stoikov_first_order_finite_horizon','formula_reference':'Avellaneda-Stoikov (2008), equations 29 and 30',
          'symbol':p.symbol,'venue':p.venue,'signal_ns':p.signal_ns,'quote_unit_coins':str(unit),
          'inventory_lots':str(q),'signed_unhedged_coins':str(p.signed_unhedged_coins),
          'mid_usdt_per_lot':str(mid),'reservation_usdt_per_lot':str(reservation),
          'inventory_shift_usdt_per_lot':str(inventory_shift),'half_spread_usdt_per_lot':str(half),
          'raw_buy_price_usdt_per_coin':str(buy),'raw_sell_price_usdt_per_coin':str(sell),
          'buy_price_ticks':bid,'sell_price_ticks':ask,'price_unit_usdt':str(p.tick_size),'native_tick_units':p.native_tick_units,
          'buy_strictly_inside':p.bid_ticks<bid<p.ask_ticks,'sell_strictly_inside':p.bid_ticks<ask<p.ask_ticks,
          'gamma_per_usdt':str(p.gamma_per_usdt),'horizon_seconds':str(p.horizon_seconds),
          'sigma2_usdt2_per_second':str(p.sigma2_usdt2_per_second),'sigma_known_through_ns':p.sigma_known_through_ns,
          'a_per_second':str(c.a_per_second),'k_per_usdt':str(c.k_per_usdt),'a_used_in_quote_formula':False,
          'predicted_buy_arrival_rate_per_second':str(c.a_per_second*(-c.k_per_usdt*(mid-buy*unit)).exp()),
          'predicted_sell_arrival_rate_per_second':str(c.a_per_second*(-c.k_per_usdt*(sell*unit-mid)).exp()),
          'calibration_trained_through_ns':c.trained_through_ns,'calibration_valid_from_ns':c.valid_from_ns,
          'calibration_valid_until_ns':c.valid_until_ns,'source_sha256':list(c.source_sha256)}
