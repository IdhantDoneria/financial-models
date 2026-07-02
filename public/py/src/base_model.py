"""Abstract base class and shared utilities for all financial models.

This module defines :class:`BaseFinancialModel`, the common interface that every
model in this package implements, plus reusable input-validation helpers and the
:class:`Benchmark` container used by the automated scoring engine.

Design goals (see ``README.md`` for the full rationale):

* **Pedagogical** – every public method carries a Google-style docstring; the
  formula it implements is stated in the docstring and echoed in inline comments.
* **Numerically honest** – models expose :meth:`reference_benchmarks` returning
  canonical, literature-sourced values so accuracy can be measured, not asserted.
* **Production ready** – strict input validation, structured logging, full type
  hints, and a single well-defined extension point (subclass + implement three
  abstract methods).
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - import only needed for static type checking
    import plotly.graph_objects as go


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class ModelError(Exception):
    """Base class for all errors raised by financial models."""


class ValidationError(ModelError, ValueError):
    """Raised when a caller supplies an invalid parameter to a model.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers keep
    working, while still being distinguishable as a domain-specific error.
    """


# --------------------------------------------------------------------------- #
# Benchmark container (consumed by the scoring engine and the test-suite)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Benchmark:
    """A single (computed vs. reference) comparison used to grade accuracy.

    Attributes:
        label: Human-readable description of what is being checked.
        computed: The value produced by the model implementation.
        expected: The reference value from academic literature / a benchmark.
        rel_tol: Maximum acceptable relative error, ``|computed-expected|/|expected|``.
        source: Citation for where ``expected`` comes from.
    """

    label: str
    computed: float
    expected: float
    rel_tol: float = 1e-9
    source: str = ""

    @property
    def rel_error(self) -> float:
        """Return the relative error, guarding against a zero reference value."""
        denom = abs(self.expected) if abs(self.expected) > 1e-15 else 1.0
        return abs(self.computed - self.expected) / denom

    @property
    def passed(self) -> bool:
        """``True`` when the relative error is within ``rel_tol``."""
        return self.rel_error <= self.rel_tol


# --------------------------------------------------------------------------- #
# Base model
# --------------------------------------------------------------------------- #
class BaseFinancialModel(abc.ABC):
    """Common interface shared by every financial model in this package.

    Subclasses must:

    1. Set the class attributes :attr:`name`, :attr:`category` and
       :attr:`references`.
    2. Accept all tunable inputs as constructor keyword arguments (never hardcode
       market parameters such as the risk-free rate).
    3. Implement :meth:`calculate`, :meth:`explain` and :meth:`visualize`.
    4. Provide :meth:`reference_benchmarks` so numerical accuracy can be scored.

    The base class supplies a configured :class:`logging.Logger` and a family of
    ``_require_*`` validators that raise :class:`ValidationError` with a clear,
    field-specific message.
    """

    #: Short display name, e.g. ``"Black-Scholes-Merton"``.
    name: str = "Base Financial Model"
    #: Category used for grouping in the navigation UI, e.g. ``"Derivatives"``.
    category: str = "General"
    #: List of academic references (author, year, title) backing the model.
    references: list[str] = []

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        """Initialise shared state.

        Args:
            logger: Optional pre-configured logger. When omitted, a module-scoped
                logger named ``financial_models.<ClassName>`` is created. A
                :class:`~logging.NullHandler` is attached so importing the library
                never emits log records unless the host application opts in.
        """
        self._logger = logger or logging.getLogger(
            f"financial_models.{type(self).__name__}"
        )
        if not self._logger.handlers:
            self._logger.addHandler(logging.NullHandler())

    # ----------------------------- interface ------------------------------ #
    @abc.abstractmethod
    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Run the model and return a dictionary of named results."""

    @abc.abstractmethod
    def explain(self) -> str:
        """Return a Markdown string deriving the formula with a worked example."""

    @abc.abstractmethod
    def visualize(self, **kwargs: Any) -> "go.Figure":
        """Return an interactive Plotly figure illustrating the model."""

    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return literature-sourced benchmarks for the scoring engine.

        The default implementation returns an empty list; every concrete model
        overrides it with canonical values (e.g. Hull's textbook option prices).
        """
        return []

    # --------------------------- validators -------------------------------- #
    @staticmethod
    def _require_positive(value: float, name: str) -> float:
        """Validate ``value > 0``.

        Args:
            value: Number to validate.
            name: Parameter name used in the error message.

        Returns:
            The validated value as a ``float``.

        Raises:
            ValidationError: If ``value`` is not a finite positive number.
        """
        v = BaseFinancialModel._as_finite_float(value, name)
        if v <= 0:
            raise ValidationError(f"{name!r} must be > 0, got {v!r}.")
        return v

    @staticmethod
    def _require_nonnegative(value: float, name: str) -> float:
        """Validate ``value >= 0`` and finite."""
        v = BaseFinancialModel._as_finite_float(value, name)
        if v < 0:
            raise ValidationError(f"{name!r} must be >= 0, got {v!r}.")
        return v

    @staticmethod
    def _require_in_range(
        value: float, name: str, low: float, high: float
    ) -> float:
        """Validate ``low <= value <= high`` and finite."""
        v = BaseFinancialModel._as_finite_float(value, name)
        if not (low <= v <= high):
            raise ValidationError(
                f"{name!r} must be in [{low}, {high}], got {v!r}."
            )
        return v

    @staticmethod
    def _require_probability(value: float, name: str) -> float:
        """Validate that ``value`` is a probability in ``[0, 1]``."""
        return BaseFinancialModel._require_in_range(value, name, 0.0, 1.0)

    @staticmethod
    def _as_finite_float(value: float, name: str) -> float:
        """Coerce ``value`` to ``float`` and reject NaN/inf/non-numeric input."""
        try:
            v = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{name!r} must be numeric, got {value!r}.") from exc
        if not np.isfinite(v):
            raise ValidationError(f"{name!r} must be finite, got {v!r}.")
        return v

    @staticmethod
    def _as_float_array(value: Sequence[float] | np.ndarray, name: str) -> np.ndarray:
        """Validate and convert a sequence to a finite 1-D float ``np.ndarray``.

        Args:
            value: Any array-like of numbers.
            name: Parameter name used in the error message.

        Returns:
            A 1-D ``float64`` array.

        Raises:
            ValidationError: If the input is empty, ragged, non-numeric, or holds
                non-finite entries.
        """
        try:
            arr = np.asarray(value, dtype=float).ravel()
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"{name!r} must be array-like of numbers, got {value!r}."
            ) from exc
        if arr.size == 0:
            raise ValidationError(f"{name!r} must be non-empty.")
        if not np.all(np.isfinite(arr)):
            raise ValidationError(f"{name!r} must contain only finite values.")
        return arr

    # ------------------------------ dunder --------------------------------- #
    def __repr__(self) -> str:  # noqa: D105 - trivial
        return f"<{type(self).__name__} name={self.name!r} category={self.category!r}>"
