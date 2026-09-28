import polars as pl


class UnknownMetricError(ValueError):
    pass


_OPS = {
    "lt": lambda col, v: col < v,
    "lte": lambda col, v: col <= v,
    "gt": lambda col, v: col > v,
    "gte": lambda col, v: col >= v,
    "eq": lambda col, v: col == v,
}


def _apply_filter(df: pl.DataFrame, f: dict) -> pl.DataFrame:
    metric, op, value = f["metric"], f["op"], f["value"]
    if metric not in df.columns:
        raise UnknownMetricError(f"unknown metric '{metric}'")
    col = pl.col(metric)
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise UnknownMetricError("'between' requires [min, max]")
        lo, hi = float(value[0]), float(value[1])
        return df.filter((col >= lo) & (col <= hi))
    if op in _OPS:
        if isinstance(value, (list, tuple)):
            raise UnknownMetricError(f"op '{op}' requires a scalar value")
        return df.filter(_OPS[op](col, float(value)))
    raise UnknownMetricError(f"unknown op '{op}'")


def run_screen(
    df: pl.DataFrame,
    filters: list[dict],
    rank_by: str | None = None,
    rank_desc: bool = True,
    limit: int = 100,
    instrument_types: list[str] | None = None,
) -> dict:
    """Apply filters, optional instrument-type filter, ranking, and limit.

    Returns {"total": rows after filters, "rows": ranked rows up to limit}.
    Nulls never match a filter and always sort last.
    """
    out = df
    for f in filters:
        out = _apply_filter(out, f)
    if instrument_types:
        if "instrument_type" not in out.columns:
            raise UnknownMetricError("instrument_type column missing from universe frame")
        out = out.filter(pl.col("instrument_type").is_in(instrument_types))
    total = out.height
    if rank_by is not None:
        if rank_by not in out.columns:
            raise UnknownMetricError(f"unknown rank metric '{rank_by}'")
        out = out.sort(rank_by, descending=rank_desc, nulls_last=True)
    rows = out.head(limit).to_dicts()
    return {"total": total, "rows": rows}
