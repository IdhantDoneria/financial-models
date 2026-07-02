"""Fama-French three-factor model with a live Ken French data loader.

Estimates the Fama-French (1993) three-factor regression of an asset's excess
returns on the market, size (SMB) and value (HML) factors, and ships a data
loader that fetches the factor series directly from Kenneth French's data
library, caches them locally, and degrades gracefully when offline.

Regression (Fama & French, 1993)::

    R_i - R_f = alpha + b_MKT (R_m - R_f) + b_SMB * SMB + b_HML * HML + e

Coefficients are estimated by ordinary least squares; standard errors, t-stats
and R^2 are computed in closed form from the normal equations.

Data source:
    Kenneth R. French — Data Library, "Fama/French 3 Factors" (monthly).
    https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np
import pandas as pd

from .base_model import BaseFinancialModel, Benchmark, ModelError, ValidationError

if TYPE_CHECKING:  # pragma: no cover
    import plotly.graph_objects as go

#: Canonical download URL for the monthly Fama-French 3-factor CSV.
FF_FACTORS_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_CSV.zip"
)
#: Factor columns exposed by :meth:`FamaFrenchModel.load_factors` (decimals, not %).
FACTOR_COLUMNS = ("Mkt-RF", "SMB", "HML", "RF")
_DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "cache" / "ff_factors.csv"


class FamaFrenchModel(BaseFinancialModel):
    """Estimate Fama-French three-factor loadings by OLS.

    Assumptions:
        * Returns and factors are aligned, same-length series in decimal units.
        * Residuals are homoskedastic (classical OLS standard errors).

    Example:
        >>> factors = FamaFrenchModel.load_factors()          # doctest: +SKIP
        >>> model = FamaFrenchModel(asset_returns=my_returns,  # doctest: +SKIP
        ...                         factors=factors.loc[my_index])
        >>> model.calculate()["beta_mkt"]                      # doctest: +SKIP
    """

    name = "Fama-French 3-Factor"
    category = "Equity / Factor"
    references = [
        "Fama, E. F. & French, K. R. (1993). Common Risk Factors in the Returns "
        "on Stocks and Bonds. Journal of Financial Economics, 33(1), 3-56.",
        "French, K. R. Data Library (accessed 2026-07-02). "
        "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html",
    ]

    def __init__(
        self,
        *,
        asset_returns: Sequence[float] | np.ndarray,
        factors: "pd.DataFrame | Mapping[str, Sequence[float]]",
        logger: Any = None,
    ) -> None:
        """Initialise the regression with aligned asset returns and factor data.

        Args:
            asset_returns: Asset total returns (decimal, e.g. 0.03 = 3%).
            factors: Object exposing ``Mkt-RF``, ``SMB``, ``HML`` and ``RF``
                columns/keys aligned 1:1 with ``asset_returns``.
            logger: Optional logger forwarded to the base class.

        Raises:
            ValidationError: If returns/factors are misaligned or a factor column
                is missing.
        """
        super().__init__(logger=logger)
        self.asset_returns = self._as_float_array(asset_returns, "asset_returns")
        frame = pd.DataFrame(factors)
        missing = [c for c in FACTOR_COLUMNS if c not in frame.columns]
        if missing:
            raise ValidationError(f"factors missing required columns: {missing}")
        if len(frame) != self.asset_returns.size:
            raise ValidationError(
                f"asset_returns (n={self.asset_returns.size}) and factors "
                f"(n={len(frame)}) must be the same length."
            )
        self.factors = frame.reset_index(drop=True)
        # Excess asset return is the OLS dependent variable.
        self._excess = self.asset_returns - self.factors["RF"].to_numpy(float)

    # ------------------------------------------------------------------ #
    # Data loader
    # ------------------------------------------------------------------ #
    @staticmethod
    def load_factors(
        cache_dir: str | Path | None = None,
        url: str = FF_FACTORS_URL,
        force_refresh: bool = False,
        timeout: float = 30.0,
    ) -> "pd.DataFrame":
        """Load monthly Fama-French factors, using a local cache when possible.

        The parsed CSV is cached so repeated calls avoid the network. On a network
        failure the cache is used as a fallback; if neither is available a
        :class:`ModelError` is raised.

        Args:
            cache_dir: Directory for the cached CSV (default: ``<repo>/data/cache``).
            url: Download URL (defaults to Ken French's 3-factor zip).
            force_refresh: If ``True``, ignore any cache and re-download.
            timeout: Per-request timeout in seconds.

        Returns:
            DataFrame indexed by integer ``YYYYMM`` with decimal columns
            ``Mkt-RF``, ``SMB``, ``HML``, ``RF``.

        Raises:
            ModelError: If the data cannot be obtained from the network or cache.
        """
        cache = Path(cache_dir) / "ff_factors.csv" if cache_dir else _DEFAULT_CACHE
        if cache.exists() and not force_refresh:
            return pd.read_csv(cache, index_col=0)
        try:
            import requests

            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                raw = zf.read(zf.namelist()[0]).decode("latin-1")
            frame = FamaFrenchModel._parse_ff_csv(raw)
        except Exception as exc:  # network / parse failure -> fall back to cache
            if cache.exists():
                return pd.read_csv(cache, index_col=0)
            raise ModelError(
                f"Could not download Fama-French factors from {url!r} and no "
                f"cache exists at {cache}: {exc}"
            ) from exc
        cache.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(cache)
        return frame

    @staticmethod
    def _parse_ff_csv(raw: str) -> "pd.DataFrame":
        """Parse Ken French's CSV text into a decimal monthly factor DataFrame.

        The file has descriptive header lines, then a monthly block of
        ``YYYYMM, Mkt-RF, SMB, HML, RF`` rows (values in percent), followed by an
        annual block. Only the monthly block is retained and converted to decimals.
        """
        rows: list[list[float]] = []
        index: list[int] = []
        for line in raw.splitlines():
            cells = [c.strip() for c in line.split(",")]
            # A monthly data row starts with a 6-digit YYYYMM token and has 5 fields.
            if len(cells) == 5 and cells[0].isdigit() and len(cells[0]) == 6:
                index.append(int(cells[0]))
                rows.append([float(c) for c in cells[1:]])
        if not rows:
            raise ModelError("No monthly Fama-French rows found in downloaded file.")
        frame = pd.DataFrame(rows, index=index, columns=list(FACTOR_COLUMNS))
        frame.index.name = "date"
        return frame / 100.0  # percent -> decimal

    # ------------------------------------------------------------------ #
    # OLS estimation
    # ------------------------------------------------------------------ #
    def _fit(self) -> dict[str, Any]:
        """Solve the OLS normal equations and return coefficients + diagnostics."""
        n = self._excess.size
        # Design matrix X = [1, Mkt-RF, SMB, HML]; k = 4 parameters.
        x = np.column_stack([
            np.ones(n),
            self.factors["Mkt-RF"].to_numpy(float),
            self.factors["SMB"].to_numpy(float),
            self.factors["HML"].to_numpy(float),
        ])
        y = self._excess
        xtx_inv = np.linalg.inv(x.T @ x)
        beta = xtx_inv @ x.T @ y                     # OLS estimator (X'X)^-1 X'y
        resid = y - x @ beta
        dof = n - x.shape[1]
        # Classical homoskedastic standard errors: s^2 (X'X)^-1.
        sigma2 = resid @ resid / dof if dof > 0 else np.nan
        se = np.sqrt(np.diag(sigma2 * xtx_inv))
        t_stats = beta / se
        ss_res = resid @ resid
        ss_tot = np.sum((y - y.mean()) ** 2)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else np.nan
        adj_r2 = 1 - (1 - r2) * (n - 1) / dof if dof > 0 else np.nan
        return {
            "beta": beta, "se": se, "t_stats": t_stats,
            "r_squared": float(r2), "adj_r_squared": float(adj_r2),
            "residuals": resid, "fitted": x @ beta,
        }

    def calculate(self, **kwargs: Any) -> dict[str, Any]:
        """Estimate factor loadings and regression diagnostics.

        Returns:
            Dict with ``alpha``, ``beta_mkt``, ``beta_smb``, ``beta_hml``,
            ``r_squared``, ``adj_r_squared`` and a ``t_stats`` mapping.
        """
        fit = self._fit()
        names = ("alpha", "beta_mkt", "beta_smb", "beta_hml")
        result: dict[str, Any] = {name: float(b) for name, b in zip(names, fit["beta"])}
        result["r_squared"] = fit["r_squared"]
        result["adj_r_squared"] = fit["adj_r_squared"]
        result["t_stats"] = {name: float(t) for name, t in zip(names, fit["t_stats"])}
        self._logger.info(
            "FF3 fit: alpha=%.5f, b_mkt=%.3f, b_smb=%.3f, b_hml=%.3f, R2=%.3f",
            result["alpha"], result["beta_mkt"], result["beta_smb"],
            result["beta_hml"], result["r_squared"],
        )
        return result

    def explain(self) -> str:
        """Return a Markdown explanation with the current worked example."""
        res = self.calculate()
        return (
            "### Fama-French Three-Factor Model\n\n"
            "Augments the CAPM with size and value premia. Excess returns are "
            "regressed on three factors:\n\n"
            r"$$R_i - R_f = \alpha + b_{MKT}(R_m-R_f) + b_{SMB}\,\text{SMB} "
            r"+ b_{HML}\,\text{HML} + \varepsilon$$"
            "\n\n- **MKT** = market excess return, **SMB** = small-minus-big (size), "
            "**HML** = high-minus-low book-to-market (value).\n"
            "- A significant $\\alpha$ indicates return unexplained by the three factors.\n\n"
            "**Worked example (current inputs):**\n"
            f"- α = {res['alpha']:.5f} (t = {res['t_stats']['alpha']:.2f})\n"
            f"- b_MKT = {res['beta_mkt']:.3f}, b_SMB = {res['beta_smb']:.3f}, "
            f"b_HML = {res['beta_hml']:.3f}\n"
            f"- R² = {res['r_squared']:.3f}, adj. R² = {res['adj_r_squared']:.3f}\n"
        )

    def visualize(self, **kwargs: Any) -> "go.Figure":
        """Plot the three estimated factor loadings as a labelled bar chart.

        Returns:
            A Plotly bar chart of ``b_MKT``, ``b_SMB``, ``b_HML`` with the alpha
            and R^2 annotated in the title.
        """
        import plotly.graph_objects as go

        res = self.calculate()
        labels = ["b_MKT", "b_SMB", "b_HML"]
        values = [res["beta_mkt"], res["beta_smb"], res["beta_hml"]]
        colors = ["#5b8def" if v >= 0 else "#f87171" for v in values]
        fig = go.Figure(go.Bar(x=labels, y=values, marker_color=colors,
                               text=[f"{v:.2f}" for v in values], textposition="outside"))
        fig.add_hline(y=0, line_color="#6b7690")
        fig.update_layout(
            title=(f"Fama-French Factor Loadings "
                   f"(α={res['alpha']:.4f}, R²={res['r_squared']:.2f})"),
            yaxis_title="Factor beta", template="plotly_white",
        )
        return fig

    @classmethod
    def reference_benchmarks(cls) -> list[Benchmark]:
        """Return an exact OLS-recovery benchmark (no network dependency)."""
        rng = np.random.default_rng(0)
        n = 240
        # Construct factors and a NOISELESS asset so OLS must recover coefficients.
        mkt, smb, hml = (rng.normal(0.01, 0.04, n), rng.normal(0.0, 0.03, n),
                         rng.normal(0.0, 0.03, n))
        rf = np.full(n, 0.002)
        true = dict(alpha=0.001, b_mkt=1.1, b_smb=0.4, b_hml=-0.3)
        excess = true["alpha"] + true["b_mkt"] * mkt + true["b_smb"] * smb + true["b_hml"] * hml
        asset = excess + rf  # invert excess = asset - RF
        factors = pd.DataFrame({"Mkt-RF": mkt, "SMB": smb, "HML": hml, "RF": rf})
        res = cls(asset_returns=asset, factors=factors).calculate()
        return [
            Benchmark("OLS recovers alpha (noiseless)", res["alpha"], true["alpha"],
                      rel_tol=1e-9, source="Fama-French (1993) exact-recovery check"),
            Benchmark("OLS recovers b_MKT (noiseless)", res["beta_mkt"], true["b_mkt"],
                      rel_tol=1e-9, source="Fama-French (1993) exact-recovery check"),
            Benchmark("OLS recovers b_HML (noiseless)", res["beta_hml"], true["b_hml"],
                      rel_tol=1e-9, source="Fama-French (1993) exact-recovery check"),
            Benchmark("R-squared == 1 for exact fit", res["r_squared"], 1.0,
                      rel_tol=1e-9, source="Noiseless linear model"),
        ]
