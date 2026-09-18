"""Deterministic portfolio-risk and capital-allocation primitives.

Data-in/data-out only. No brokerage submission path. The research layer supplies
verified IBKR snapshots and current external observations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

@dataclass(frozen=True)
class AssignmentRow:
    underlying: str; currency: str; contracts: int; strike: float; expiry: str; assignment_notional: float; spot: float | None; distance_to_strike: float | None; days_to_expiry: int | None

def _days(expiry: str, as_of: str | None) -> int | None:
    if not as_of: return None
    return (date.fromisoformat(expiry) - date.fromisoformat(as_of[:10])).days

def assignment_ledger(positions: Iterable[dict[str, Any]], as_of: str | None = None) -> list[AssignmentRow]:
    rows=[]
    for p in positions:
        contracts=int(p.get("contracts",0))
        if contracts<=0 or p.get("side","SELL").upper()!="SELL" or p.get("right","P").upper()!="P": continue
        strike=float(p["strike"]); multiplier=float(p.get("contract_multiplier",100)); expiry=str(p["expiry"]); spot=float(p["spot"]) if p.get("spot") is not None else None
        distance=(spot/strike-1.0) if spot is not None and strike else None
        rows.append(AssignmentRow(str(p["underlying"]),str(p.get("currency","USD")),contracts,strike,expiry,contracts*strike*multiplier,spot,distance,_days(expiry,as_of)))
    return sorted(rows,key=lambda r:(r.days_to_expiry if r.days_to_expiry is not None else 10**9,r.underlying,r.strike))

def aggregate_assignment(rows: Iterable[AssignmentRow])->dict[str,dict[str,float]]:
    """Per-underlying assignment exposure, with the currency carried through.

    Grouping is by underlying, and one underlying is normally one currency,
    so this does not mix units the way a cross-underlying total does. The
    currency is reported anyway: a bare number with no unit attached is how
    the mixing happened in the first place, and a caller summing these
    values needs to know what they are denominated in.
    """
    out={}
    for r in rows:
        item=out.setdefault(r.underlying,{"contracts":0.0,"assignment_notional":0.0,"currency":r.currency})
        if item["currency"]!=r.currency:
            raise ValueError(f"mixed_currencies_for_underlying:{r.underlying}:{sorted({item['currency'],r.currency})}")
        item["contracts"]+=r.contracts; item["assignment_notional"]+=r.assignment_notional
    return out

def _require_single_currency(rows:Sequence[AssignmentRow],function:str)->None:
    """Refuse to add different currencies together without rates.

    USD 10,000 plus EUR 10,000 was reported as 20,000, silently, as though
    both legs were the same unit. At EURUSD 1.10 the true figure is 21,000,
    so the exposure was understated and the error scales with the
    non-base book.

    normalize_currency already exists in this module and raises on a
    missing rate. The aggregators simply were not using it. Passing
    fx_to_base makes them consistent with that; refusing a mixed book with
    no rates is the same fail-closed behaviour normalize_currency already
    has.
    """
    currencies={r.currency for r in rows}
    if len(currencies)>1:
        raise ValueError(
            f"mixed_currencies_without_fx:{function}:{sorted(currencies)}: "
            "supply fx_to_base to convert, or aggregate one currency at a time"
        )


def assignment_by_expiry_bucket(rows: Iterable[AssignmentRow],fx_to_base:dict[str,float]|None=None,base:str="USD")->dict[str,float]:
    rows=list(rows)
    if fx_to_base is None: _require_single_currency(rows,"assignment_by_expiry_bucket")
    buckets={"overdue":0.0,"0-30d":0.0,"31-90d":0.0,"91-365d":0.0,"1y+":0.0,"unknown":0.0}
    for r in rows:
        amount=r.assignment_notional if fx_to_base is None else normalize_currency(r.assignment_notional,r.currency,fx_to_base,base)
        d=r.days_to_expiry; key="unknown" if d is None else "overdue" if d<0 else "0-30d" if d<=30 else "31-90d" if d<=90 else "91-365d" if d<=365 else "1y+"; buckets[key]+=amount
    return buckets

def normalize_currency(amount:float,currency:str,fx_to_base:dict[str,float],base:str="USD")->float:
    if currency==base:return amount
    if currency not in fx_to_base:raise ValueError(f"missing FX rate for {currency}->{base}")
    return amount*float(fx_to_base[currency])

def normalized_assignment_total(rows:Iterable[AssignmentRow],fx_to_base:dict[str,float],base:str="USD")->float:
    return sum(normalize_currency(r.assignment_notional,r.currency,fx_to_base,base) for r in rows)

def _known(snapshot:dict[str,float],field:str)->float|None:
    """A supplied number, or None. Absence is not zero.

    These fields defaulted to 0.0 when missing, so a snapshot that simply
    did not carry excess_liquidity reported excess_liquidity = 0.0 --
    indistinguishable from an account genuinely at zero, which is a
    margin-call condition. "We do not know" and "it is nothing" are
    different facts and only one of them is alarming.

    economic_risk_capacity already used None for exactly this reason; the
    neighbouring fields just did not follow it.
    """
    value=snapshot.get(field)
    if value is None: return None
    try: return float(value)
    except (TypeError,ValueError): return None


def risk_capacity(snapshot:dict[str,float],assignment_base:float)->dict[str,float|None]:
    nav=float(snapshot["nav"]); economic=snapshot.get("economic_risk_capacity")
    # A ratio against zero or negative NAV is undefined, not zero. Reporting
    # 0.0 said "no assignment exposure relative to capital" for an account
    # with obligations and no capital, which is the opposite of the truth.
    # `nav>0` is True for infinity, and infinity divides to exactly 0.0 --
    # "no assignment exposure relative to capital", the single most
    # reassuring answer available, produced from a NAV that is not a
    # measurement. NaN and negative already returned None; infinity slipped
    # through because it is the one unusable value that is also positive.
    assignment_to_nav=assignment_base/nav if (math.isfinite(nav) and nav>0) else None
    return {"cash_capacity":_known(snapshot,"total_cash"),"margin_capacity":_known(snapshot,"available_funds"),"assignment_notional":assignment_base,"assignment_to_nav":assignment_to_nav,"gross_leverage":_known(snapshot,"leverage"),"excess_liquidity":_known(snapshot,"excess_liquidity"),"economic_risk_capacity":float(economic) if economic is not None else None,"assignment_to_economic_capacity":assignment_base/float(economic) if economic not in (None,0) else None}

def stress_nav(nav:float,shocks:Iterable[float])->list[dict[str,float]]:
    return [{"shock":float(s),"nav_after_shock":nav*(1.0+float(s)),"drawdown":float(s)} for s in shocks]

def correlated_stress(positions:Iterable[dict[str,Any]],nav:float,factor_shocks:dict[str,float],loadings:dict[str,dict[str,float]],correlation:dict[str,dict[str,float]]|None=None)->dict[str,Any]:
    """Apply correlated factor shocks to position exposures.

    `loadings[position_id][factor]` is a verified exposure. If a correlation
    matrix is supplied, the covariance-weighted shock norm is reported as a
    risk diagnostic; P&L itself remains the explicit linear factor shock.
    """
    pnl=0.0; exposure={}; missing=[]
    for p in positions:
        pid=str(p.get("position_id",p.get("symbol",p.get("underlying","unknown"))))
        value=float(p.get("market_value",0.0)); factors=loadings.get(pid)
        if factors is None: missing.append(pid); continue
        row=0.0
        for factor,loading in factors.items():
            shock=factor_shocks.get(factor)
            if shock is None: missing.append(f"{pid}:{factor}"); continue
            row+=value*float(loading)*float(shock); exposure[factor]=exposure.get(factor,0.0)+value*float(loading)
        pnl+=row
    covariance_risk=None
    if correlation is not None:
        factors=list(factor_shocks); covariance_risk=0.0
        for a in factors:
            for b in factors:
                covariance_risk+=float(exposure.get(a,0.0))*float(exposure.get(b,0.0))*float(correlation.get(a,{}).get(b,0.0))
        covariance_risk=max(covariance_risk,0.0)**0.5
    return {"pnl":pnl,"nav_after":nav+pnl,"drawdown":pnl/nav if nav else 0.0,"factor_exposure":exposure,"correlated_risk_norm":covariance_risk,"unmodeled":sorted(set(missing))}

def option_delta_exposure(position:Mapping[str,Any])->float|None:
    """Signed underlying-equivalent exposure of an option position, or None.

    Delta is d(option price)/d(underlying price). It converts a move in the
    UNDERLYING into a move in the option, so the quantity a percentage
    shock applies to is the underlying-equivalent notional:

        delta x contracts x multiplier x underlying_price

    Stress previously used ``market_value * delta * shock``, which applies
    a percentage to the option's own premium. That is dimensionally wrong
    and it understates risk badly. Ten $100-underlying calls at delta 0.5
    and $3 premium carry $50,000 of underlying exposure; a 10% move is
    about $5,000. The old expression returned $150, roughly 33x too small,
    and stress output is exactly where an understatement is least
    survivable.

    Returns None when the inputs needed for an honest number are absent.
    Callers count that as unmodeled notional rather than substituting a
    wrong one -- a known gap in coverage beats a confident bad figure.

    A caller that has already computed exposure elsewhere may supply
    ``delta_exposure`` directly and it is used as given.
    """
    direct=position.get("delta_exposure")
    if direct is not None:
        try: return float(direct)
        except (TypeError,ValueError): return None
    delta=position.get("delta")
    if delta is None: return None
    underlying=position.get("underlying_price")
    if underlying is None: return None
    # Two position schemas exist in this codebase and they encode direction
    # differently. A signed `quantity` carries its own sign. The
    # assignment/opportunity schema uses a POSITIVE `contracts` plus a
    # separate `side`, so reading size without reading side silently turns
    # every short option into a long one.
    #
    # That inverts the sign of the stress result: a short put on a -10%
    # move reported +$500 when it loses $500. A sign error in risk output
    # is worse than the magnitude error this helper was written to fix,
    # because it points the wrong way rather than merely understating.
    quantity=position.get("quantity")
    if quantity is None:
        contracts=position.get("contracts")
        if contracts is None: return None
        side=str(position.get("side","BUY")).upper()
        if side in ("SELL","SHORT","S","WRITE"): quantity=-abs(float(contracts))
        elif side in ("BUY","LONG","B",""): quantity=abs(float(contracts))
        else: return None
    try:
        # contract_multiplier is the name used by assignment_ledger and
        # covered_call_opportunity; multiplier is used elsewhere. Honour
        # whichever is present rather than defaulting past a supplied one.
        raw_multiplier=position.get("contract_multiplier",position.get("multiplier",100))
        return float(delta)*float(quantity)*float(raw_multiplier)*float(underlying)
    except (TypeError,ValueError):
        return None


def scenario_stress(positions:Iterable[dict[str,Any]],nav:float,scenarios:Iterable[dict[str,Any]])->list[dict[str,Any]]:
    # Materialize BOTH before the nested walk. positions is re-iterated once
    # per scenario, so a generator was consumed by the first scenario and
    # every later one saw an empty portfolio -- reporting pnl=0 for the
    # worst cases. A -50% crash scenario came back as zero loss, which is
    # the most dangerous possible direction for a stress number to be wrong.
    positions=list(positions); scenarios=list(scenarios)
    out=[]
    for scenario in scenarios:
        equity=float(scenario.get("equity_shock",0.0)); sector=scenario.get("sector_shocks",{}); country=scenario.get("country_shocks",{}); fx=scenario.get("fx_shocks",{}); pnl=0.0; modeled=0.0; unmodeled=0.0
        for p in positions:
            value=float(p.get("market_value",0.0)); asset=str(p.get("asset_class","STK")).upper()
            if asset=="OPT":
                exposure=option_delta_exposure(p)
                if exposure is None: unmodeled+=abs(value); continue
                row_shock=float(p.get("underlying_shock",equity)); row_pnl=exposure*row_shock; modeled+=abs(exposure)
            else:
                row_shock=float(sector.get(str(p.get("sector","")),country.get(str(p.get("country","")),equity))); row_pnl=value*float(p.get("beta",1.0))*row_shock; modeled+=abs(value)
            currency=str(p.get("currency","USD"));
            if currency in fx: row_pnl+=value*float(fx[currency])
            pnl+=row_pnl
        assignment=float(scenario.get("assignment_notional",0.0)); assignment_shock=float(scenario.get("assignment_shock",equity)); assignment_pnl=-assignment*max(-assignment_shock,0.0); pnl+=assignment_pnl; modeled+=assignment
        out.append({"name":scenario.get("name","unnamed"),"pnl":pnl,"nav_after":nav+pnl,"drawdown":pnl/nav if nav else 0.0,"assignment_pnl":assignment_pnl,"modeled_notional":modeled,"unmodeled_notional":unmodeled,"coverage_ratio":modeled/max(modeled+unmodeled,1e-12)})
    return out
