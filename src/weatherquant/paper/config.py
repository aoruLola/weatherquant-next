"""Paper-runtime configuration: paths, cadence, and the enablement gate.

One knob here is qualitatively different from the rest:

``enabled`` (``WQ_PAPER_ENABLED``)
    **Default False.** Every other setting only shapes *how* the simulation
    runs; this one decides whether it runs at all. A paper engine that came
    up because its defaults allowed it would be an enablement decision
    nobody made, so starting one requires the operator to set
    ``WQ_PAPER_ENABLED=true`` explicitly - and that act is the approval the
    specification asks for. The CLI refuses a disabled start with a named
    error rather than a default-on fallback.

Field names carry no ``paper_`` prefix on purpose: pydantic-settings
prepends ``WQ_PAPER_`` to the field name, so ``db_path`` reads
``WQ_PAPER_DB_PATH`` - the same convention that makes ``shadow_db_path``
read ``WQ_SHADOW_DB_PATH``.

Paths follow the collector/shadow convention (environment variables,
``.env`` file). There is deliberately *no* thresholds file yet: risk gates
arrive with WQN-43, which is the ticket that has a reason to argue about
their values.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["PaperSettings", "parse_bool_flag"]

#: The strings ``WQ_PAPER_ENABLED`` accepts. Restricted on purpose: pydantic's
#: generic bool parser also admits ``0``/``yes``/``on``/``y``, and an
#: enablement gate that can be opened by accident is not a gate. Turning the
#: engine on must look deliberate in a ``.env`` file.
_TRUE_FLAGS = frozenset({"true", "1", "enabled"})
_FALSE_FLAGS = frozenset({"false", "0", "disabled", ""})


def parse_bool_flag(raw: str | bool | None) -> bool:
    """Parse the enablement flag strictly, refusing anything ambiguous.

    ``None`` (unset) is False: absent approval is no approval. An
    unrecognised *non-empty* string raises rather than falling through to a
    default, because ``WQ_PAPER_ENABLED=ture`` must be a typo the operator
    sees, not a disabled engine they believe is running.
    """
    if raw is None or raw is False:
        return False
    if raw is True:
        return True
    text = str(raw).strip().lower()
    if text in _TRUE_FLAGS:
        return True
    if text in _FALSE_FLAGS:
        return False
    raise ValueError(
        f"WQ_PAPER_ENABLED must be one of {sorted(_TRUE_FLAGS | _FALSE_FLAGS)}, got {raw!r}; "
        "an unrecognised value is refused rather than read as 'off'"
    )


class PaperSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WQ_PAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- the enablement gate ----------------------------------------------
    enabled: bool = Field(
        default=False,
        description="Whether the paper engine may run at all. Default False; "
        "production enablement is a separate approval, expressed by setting "
        "WQ_PAPER_ENABLED=true explicitly. The CLI refuses a disabled start "
        "by name.",
    )

    # --- storage (the paper engine's own directory; never another engine's) --
    db_path: Path = Field(
        default=Path("data/paper/paper.duckdb"),
        description="The paper engine's own DuckDB file. Must never point at "
        "another engine's database; PaperDatabase refuses a foreign file by "
        "name on open.",
    )
    health_dir: Path = Field(
        default=Path("data/paper/health"),
        description="Heartbeat directory for the container probe.",
    )

    # --- inputs (published channels, read-only) -----------------------------
    events_dir: Path = Field(
        default=Path("data/shadow/reports/decisions-export"),
        description="The shadow engine's published decisions export (JSONL "
        "parts). The ONLY channel decisions reach this engine through; the "
        "shadow's database is never opened.",
    )
    raw_dir: Path = Field(
        default=Path("data/raw"),
        description="The collector's raw archive, read-only, for resolving the "
        "point-in-time L2 book a signal references.",
    )

    # --- the virtual account -------------------------------------------------
    initial_capital_usdc: float = Field(
        default=100.0,
        gt=0.0,
        description="The virtual account's opening capital, in USDC. First "
        "write wins: re-initialising with a different value is refused "
        "rather than silently rebasing the account.",
    )

    # --- cadence -------------------------------------------------------------
    tick_seconds: float = Field(
        default=5.0,
        gt=0.0,
        description="Main loop period.",
    )

    # --- consumption gates -----------------------------------------------------
    max_signal_age_seconds: float = Field(
        default=600.0,
        gt=0.0,
        description="A LIVE ACCEPT older than this at consumption time is "
        "refused (STALE_SIGNAL) rather than filled. The shadow exports at "
        "most every shadow_export_seconds (300s default), so the floor is "
        "the export cadence itself; 600s tolerates one full missed export "
        "without treating a lagging channel as a tradeable signal.",
    )
    max_book_age_seconds: float = Field(
        default=90.0,
        gt=0.0,
        description="A resolved point-in-time book older than this at the "
        "decision instant is refused (BOOK_STALE). Matches the shadow's "
        "max_snapshot_age_ms gate (v2: two measured collector sweeps), so "
        "the paper engine never fills a book the decision layer would have "
        "refused.",
    )

    # --- fill gates -------------------------------------------------------------
    require_complete_fill: bool = Field(
        default=True,
        description="A partial fill is not a smaller trade, it is a different "
        "trade: the unfilled remainder would have to be repriced against a "
        "book we just drove through. Refusing keeps every fill interpretable.",
    )
    execution_buffer_fraction: float = Field(
        default=0.005,
        ge=0.0,
        description="Execution buffer as a fraction of fill notional (0.5%), "
        "matching the collector's execution_buffer_fraction so both engines "
        "cost the same trade the same way.",
    )
    fill_batch: int = Field(
        default=100,
        ge=1,
        description="Max signals priced per fill pass. One transaction per "
        "batch; bounded like every other loop in this engine.",
    )
    archive_tail_bytes: int = Field(
        default=4 * 1024 * 1024,
        gt=0,
        description="How deep into each archived day file the point-in-time "
        "book resolver may read, from the end. LIVE decisions are "
        "contemporary, so the matching frame is near the tail; a frame "
        "outside the window is honestly not found rather than guessed at.",
    )

    # --- risk gates -------------------------------------------------------------
    risk_rules_path: Path = Field(
        default=Path("configs/paper-risk.json"),
        description="The versioned risk-rule file (per-opportunity budget, "
        "per-market exposure). A file, not environment variables: a cap that "
        "can be widened on the box producing the results cannot be audited "
        "afterwards. Missing keys are errors, never permissive defaults.",
    )

    @field_validator("enabled", mode="before")
    @classmethod
    def _strict_bool(cls, v: object) -> bool:
        # pydantic's own bool parser admits yes/on/y; the enablement gate
        # uses the stricter vocabulary, applied before pydantic ever sees
        # the raw value.
        return parse_bool_flag(v)  # type: ignore[arg-type]

    @field_validator("db_path", "events_dir", "raw_dir")
    @classmethod
    def _paths_not_empty(cls, v: Path) -> Path:
        if not str(v).strip():
            raise ValueError("paper paths must not be empty")
        return v
