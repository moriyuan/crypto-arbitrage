"""Predeclared two-date parameter ranking, without evaluation-date input."""
from decimal import Decimal


def choose(rows, fit_dates):
    if len(fit_dates) != 2 or len(set(fit_dates)) != 2:
        raise ValueError('exactly two distinct fit dates required')
    ranked = []
    identifiers = set()
    for source in rows:
        if source['id'] in identifiers:
            raise ValueError('duplicate policy')
        identifiers.add(source['id'])
        if set(source['days']) != set(fit_dates):
            raise ValueError('selection may consume only the two declared fit dates')
        days = [source['days'][date] for date in fit_dates]
        if not all(day['cycles'] >= 10 and day['unresolved_qty'] == 0
                   and day['funding_status'] == 'VERIFIED_NO_SETTLEMENT'
                   and day['accounting_pass'] is True for day in days):
            continue
        nets = [Decimal(day['net']) for day in days]
        drawdowns = [Decimal(day['drawdown']) for day in days]
        if not all(x.is_finite() for x in nets + drawdowns) or any(x < 0 for x in drawdowns):
            raise ValueError('non-finite or invalid research metric')
        ranked.append({**source, 'worst_date_net': str(min(nets)),
                       'sum_net': str(sum(nets)), 'sum_drawdown': str(sum(drawdowns)),
                       'profitable_both_fit_days': min(nets) > 0})
    ranked.sort(key=lambda row: (-Decimal(row['worst_date_net']), -Decimal(row['sum_net']),
                                Decimal(row['sum_drawdown']), row['id']))
    return (ranked[0] if ranked else None), ranked
