"""Generate the per-model calculator landing pages (and the sitemap).

Why these pages exist
---------------------
The terminal at ``/`` is one URL. Search traffic for this product is not
"financial modelling" — that SERP is owned by Wikipedia, CFI and Wall Street
Prep and is a learn/certify intent, not a tool intent — it is a long tail of
*tool-seeking* queries ("reverse dcf calculator", "operating lease
capitalization"). Ranking for those needs one indexable URL per query with
real content on it; a single-page WebAssembly app has nowhere to put that.
Each page here is that landing surface, and deep-links into the live model
via ``/?m=<MNEMONIC>`` (see ``requestedModel()`` in assets/terminal.js).

Why a generator instead of four hand-written files
--------------------------------------------------
Same reason ``build_notebook.py`` and ``sync_web_assets.py`` exist: the parts
that MUST stay identical across pages (breadcrumb markup, the JSON-LD graph
shape, canonical/OG wiring, the nav, the sitemap entry) are exactly the parts
that rot when copy-pasted. Content lives in ``PAGES``; structure lives in one
template. ``tests/test_landing_pages.py`` fails the build if the committed
HTML ever drifts from what this script produces.

    python scripts/build_landing_pages.py          # rewrite the pages
    python scripts/build_landing_pages.py --check   # verify, write nothing

Accuracy note: every formula, worked example and benchmark below is taken
from the actual model source in ``src/`` — not paraphrased from memory. If a
model changes, these numbers must change with it.
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import sys

SITE = "https://financial-models-six.vercel.app"
PUBLIC = pathlib.Path(__file__).resolve().parent.parent / "public"

#: Pages that are not generated here but must still appear in the sitemap.
#: (loc, changefreq, priority, lastmod)
STATIC_PAGES = [
    ("/", "weekly", "1.0", "2026-09-14"),
    ("/about", "monthly", "0.9", "2026-09-11"),
    ("/login", "yearly", "0.5", "2026-09-11"),
    ("/privacy", "yearly", "0.3", "2026-09-12"),
    ("/terms", "yearly", "0.3", "2026-09-12"),
    ("/cookies", "yearly", "0.3", "2026-09-12"),
]

LASTMOD = "2026-09-14"

# --------------------------------------------------------------------------
# Page content. `plan` is the honest entitlement for that model: RDCF and
# HDEBT are Analyst Pro features (see PREMIUM_MODELS in assets/terminal.js and
# the server-side gate in api/premium.py), so their pages say so instead of
# advertising a free tool that dead-ends at a paywall.
# --------------------------------------------------------------------------
PAGES: list[dict] = [
    {
        "slug": "reverse-dcf-calculator",
        "what_is_heading": "What a reverse DCF actually tells you",
        "mnemonic": "RDCF",
        "plan": "pro",
        "title": "Reverse DCF Calculator — Market-Implied Growth Rate",
        "description": (
            "Reverse DCF calculator: give it a share price and it solves for the free-cash-flow "
            "growth rate the market already implies, using a real bracketed root-finder."
        ),
        "eyebrow": "Market-implied valuation",
        "h1": "Reverse DCF Calculator",
        "lede": (
            "Instead of guessing a growth rate to produce a price, a reverse DCF takes the price "
            "the market is already quoting and solves backwards for the growth rate that price "
            "implies. It turns valuation from a forecast into a testable question: "
            "<em>do I believe the market's number?</em>"
        ),
        "what_is": [
            "A conventional discounted cash flow model asks you to supply a growth assumption and "
            "returns an intrinsic value. That makes the output only as good as the input nobody can "
            "actually know, and it makes it very easy to reverse-engineer whatever answer you were "
            "hoping for by nudging the growth rate.",
            "A <strong>reverse DCF</strong> inverts the relationship. It holds the discount rate and "
            "terminal growth fixed, takes the current market price as given, and solves for the "
            "constant free-cash-flow CAGR that would be required to justify that price. The result "
            "is not a valuation — it is a statement of what the market currently believes.",
            "This is the method Rappaport and Mauboussin call <em>expectations investing</em>: you "
            "stop competing on whose forecast is better and start asking whether the embedded "
            "forecast is plausible at all. A stock implying 34% annual free-cash-flow growth for "
            "five years is making a claim you can check against the company's own history, its "
            "addressable market, and its competition.",
        ],
        "formula": (
            "EV(price)  =  price × shares_outstanding  +  net_debt\n"
            "\n"
            "solve g such that:\n"
            "\n"
            "                 ⁿ    FCF₀(1+g)ᵗ\n"
            "  EV(g)  =       Σ   ────────────   +   PV(terminal value)   =   EV(price)\n"
            "                t=1     (1+r)ᵗ"
        ),
        "vars": [
            ("g", "the implied free-cash-flow CAGR being solved for — the output"),
            ("FCF₀", "base-year free cash flow"),
            ("r", "discount rate (WACC)"),
            ("n", "explicit forecast horizon, 5 years by default"),
            ("net_debt", "total debt less cash, bridging equity value to enterprise value"),
        ],
        "method": [
            "Enterprise value is <strong>monotonically increasing</strong> in the growth rate for a "
            "positive base free cash flow — more growth always means more value — so the root is "
            "unique and a bracketed solver is guaranteed to find it. This calculator uses Brent's "
            "method (<code>scipy.optimize.brentq</code>) over a search bracket of −50% to +200% a "
            "year: wide enough to cover a distressed business and a hyper-growth story stock, "
            "without pretending a market can imply something outside any plausible range.",
            "That matters because most free \"reverse DCF\" tools are not solvers at all — they "
            "interpolate a lookup table or iterate a fixed number of times and stop. Here the real "
            "SciPy routine runs in your browser, on CPython compiled to WebAssembly, and converges "
            "to the actual root.",
            "The model also translates the implied growth into an <strong>implied share of a "
            "disclosed total addressable market</strong>, which is a second and independent way to "
            "sanity-check the same expectation: a growth rate that quietly requires the company to "
            "capture 60% of its own TAM is usually the more obvious tell.",
        ],
        "steps": [
            "Enter the current share price and shares outstanding — together these give equity value.",
            "Add net debt (total debt less cash) to bridge from equity value to enterprise value.",
            "Enter base-year free cash flow. This is the figure the growth rate compounds from, so "
            "a one-off working-capital swing here distorts everything downstream.",
            "Set the discount rate and terminal growth. Terminal growth should not exceed the "
            "long-run risk-free rate — a company cannot outgrow its economy forever.",
            "Optionally supply base revenue and total addressable market to get the implied TAM "
            "capture alongside the implied growth rate.",
            "Read the implied CAGR, then go check it against the company's actual historical growth.",
        ],
        "example_intro": (
            "A company trades at $42.00 with 100 million shares outstanding and $200 million of net "
            "debt. Base free cash flow is $100 million, and we discount at 10% with 3% terminal growth."
        ),
        "example_rows": [
            ("Share price", "$42.00"),
            ("Shares outstanding", "100m"),
            ("Net debt", "$200m"),
            ("Base free cash flow", "$100m"),
            ("Discount rate (WACC)", "10%"),
            ("Terminal growth", "3%"),
            ("Implied enterprise value", "$4,400m"),
        ],
        "example_outro": (
            "Equity value is 42 × 100m = $4,200m; adding $200m of net debt gives an enterprise value "
            "of $4,400m. The solver then finds the single growth rate that makes the discounted cash "
            "flows sum to exactly that figure. The number it returns is the market's embedded "
            "assumption — and the useful next step is always the same: compare it with what the "
            "company has actually delivered over the last five years."
        ),
        "assumptions": [
            "Growth is modelled as a <strong>constant</strong> CAGR across the forecast horizon. Real "
            "cash flows are lumpy; this deliberately answers \"what single rate is equivalent\", not "
            "\"what is the year-by-year path\".",
            "The discount rate and terminal growth are held fixed. The implied growth rate is only "
            "meaningful relative to those two inputs — a different WACC gives a different answer for "
            "the same price, which is why they should be stated whenever the output is quoted.",
            "A negative or near-zero base free cash flow breaks the monotonicity the solver relies "
            "on. Pre-profit companies are not a good fit for this method.",
            "The output describes the market's aggregate expectation. It is not a recommendation, "
            "and a high implied growth rate is not by itself evidence that something is overvalued.",
        ],
        "faq": [
            ("What is a reverse DCF?",
             "A reverse DCF takes a company's current market price as the input and solves for the "
             "future cash-flow growth rate that price implies, rather than taking a growth forecast "
             "and producing a target price. It tells you what the market already believes."),
            ("How is a reverse DCF different from a normal DCF?",
             "A normal DCF runs assumptions → value. A reverse DCF runs value → assumptions. Same "
             "identity, solved in the opposite direction, which removes the single most "
             "manipulable input from the process."),
            ("What is a good implied growth rate?",
             "There is no universally good number. The test is comparative: an implied rate below "
             "the company's own historical growth suggests the market is sceptical; one far above "
             "it means the price requires an acceleration that has to be explained by something."),
            ("Does this calculator use a real solver?",
             "Yes. It root-finds with Brent's method via scipy.optimize.brentq over a −50% to +200% "
             "bracket, running as real CPython in your browser through WebAssembly — not a "
             "JavaScript approximation or a lookup table."),
            ("Can I use a reverse DCF on an unprofitable company?",
             "Not reliably. The solver depends on enterprise value increasing monotonically with "
             "the growth rate, which requires a positive base free cash flow. With negative base "
             "FCF the relationship inverts and the implied rate stops being interpretable."),
        ],
        "related": ["ifrs-16-hidden-debt-calculator", "monte-carlo-stock-price-simulator",
                    "black-scholes-calculator"],
        "refs": [
            "Rappaport, A. &amp; Mauboussin, M. J. (2001). <em>Expectations Investing: Reading Stock "
            "Prices for Better Returns.</em> Harvard Business School Press.",
            "Damodaran, A. (2012). <em>Investment Valuation</em>, 3rd ed., ch. 12 — the enterprise-"
            "value identity inverted here.",
        ],
    },
    {
        "slug": "ifrs-16-hidden-debt-calculator",
        "what_is_heading": "Why reported net debt understates real leverage",
        "mnemonic": "HDEBT",
        "plan": "pro",
        "title": "Operating Lease Capitalization & Hidden Debt Calculator",
        "description": (
            "Capitalize operating leases and move footnote liabilities onto the balance sheet: "
            "lease debt, reverse factoring and contingent liabilities, the Damodaran way."
        ),
        "eyebrow": "Forensic accounting",
        "h1": "Operating Lease Capitalization &amp; Hidden-Debt Calculator",
        "lede": (
            "Reported net debt is not total leverage. This tool present-values remaining lease "
            "commitments, adds disclosed reverse-factoring exposure and probability-weighted "
            "contingent liabilities, and shows you what the balance sheet would look like if the "
            "footnotes were on it."
        ),
        "what_is": [
            "Three categories of economically real borrowing routinely sit outside reported net debt, "
            "in the notes to the accounts rather than on the face of the balance sheet. Ratio "
            "analysis that starts from the reported figure silently understates leverage for exactly "
            "the companies where it matters most — retail, airlines, logistics, hospitality.",
            "<strong>Capitalised operating leases.</strong> Ind AS 116 and IFRS 16 brought most "
            "leases on balance sheet, but comparability across periods, across reporting standards, "
            "and across companies with different disclosure practice still requires doing the "
            "present-value calculation yourself.",
            "<strong>Reverse factoring / supply-chain finance.</strong> Economically this is "
            "borrowing; it is frequently presented within trade payables, which flatters both net "
            "debt and operating cash flow. Where the exposure is disclosed, it belongs in debt.",
            "<strong>Contingent liabilities.</strong> Usually treated as a binary — either ignored "
            "entirely or taken at full face value. Neither is right. The defensible treatment is to "
            "probability-weight the disclosed amount.",
            "This is the adjustment a credit or equity analyst does by hand from the notes. Here it "
            "is a formula applied to a real filing.",
        ],
        "formula": (
            "Capitalised lease liability (present value of an ordinary annuity):\n"
            "\n"
            "            1 − (1 + r)⁻ⁿ\n"
            "  L  =  C × ──────────────\n"
            "                  r\n"
            "\n"
            "  hidden_debt    =  L  +  reverse_factoring  +  Σ (contingentᵢ × probabilityᵢ)\n"
            "  adjusted_debt  =  reported_net_debt  +  hidden_debt"
        ),
        "vars": [
            ("C", "annual lease payment"),
            ("r", "incremental borrowing rate — the rate the company itself would pay"),
            ("n", "remaining lease term, in years"),
            ("L", "the capitalised lease liability, treated as debt"),
        ],
        "method": [
            "The lease liability is a plain ordinary-annuity present value, discounted at the "
            "<strong>incremental borrowing rate</strong> rather than a market-wide rate — the whole "
            "point is what this borrower would pay. Using a generic risk-free rate here understates "
            "the liability for precisely the weaker credits where the adjustment matters.",
            "Contingent liabilities are probability-weighted rather than included or excluded "
            "wholesale, so a ₹500 crore claim assessed at 20% likelihood contributes ₹100 crore of "
            "expected liability instead of either ₹0 or ₹500 crore.",
            "The same model also recomputes Buffett-style <strong>owner earnings</strong>, which "
            "reverses the smoothing effect of capitalised R&amp;D: the amortisation already embedded "
            "in reported net income is added back, and the period's actual cash R&amp;D spend is "
            "expensed in full.",
        ],
        "owner_earnings": (
            "owner_earnings  =  net_income + D&amp;A + rd_amortization − rd_cash_spend − maint_capex"
        ),
        "steps": [
            "Take the annual lease payment and remaining lease term from the lease note.",
            "Use the company's incremental borrowing rate — often disclosed in the same note. If it "
            "is not, the marginal cost of debt is the closest defensible proxy.",
            "Add any disclosed reverse-factoring or supply-chain-finance exposure.",
            "List contingent liabilities with a probability for each, rather than accepting or "
            "discarding them wholesale.",
            "Compare adjusted net debt with the reported figure, then recompute the leverage ratios "
            "that actually drive the credit view.",
        ],
        "example_intro": (
            "A company reports ₹1,000 crore of net debt. Its lease note shows ₹120 crore of annual "
            "payments with 8 years remaining, at an 9% incremental borrowing rate. It discloses "
            "₹150 crore of supply-chain finance and a ₹500 crore contingent claim assessed at 20%."
        ),
        "example_rows": [
            ("Reported net debt", "₹1,000cr"),
            ("Annual lease payment (C)", "₹120cr"),
            ("Remaining term (n)", "8 years"),
            ("Incremental borrowing rate (r)", "9%"),
            ("Capitalised lease liability (L)", "≈ ₹664cr"),
            ("Reverse factoring", "₹150cr"),
            ("Contingent liability (₹500cr × 20%)", "₹100cr"),
            ("Adjusted net debt", "≈ ₹1,914cr"),
        ],
        "example_outro": (
            "Reported leverage understates the real figure by roughly 91% in this example. Nothing "
            "here is an accounting irregularity — every input was disclosed. It was simply disclosed "
            "in the notes rather than in the number most screens read."
        ),
        "assumptions": [
            "Lease payments are treated as a level annuity. Where the maturity schedule is disclosed "
            "year by year and is materially uneven, discounting the actual schedule is more precise.",
            "The result is only as good as the incremental borrowing rate you supply — it is the "
            "single input the present value is most sensitive to.",
            "Probability weights on contingent liabilities are a judgement. They should be stated "
            "openly alongside the output, because a reader may reasonably disagree with them.",
            "This is an analytical restatement for valuation and credit work. It is not a statutory "
            "accounting treatment and does not replace the reported financial statements.",
        ],
        "faq": [
            ("How do you capitalize operating leases?",
             "Discount the remaining lease payments to present value at the lessee's incremental "
             "borrowing rate and add the result to net debt. For a level payment stream that is the "
             "ordinary-annuity formula L = C × (1 − (1+r)^−n) / r."),
            ("What discount rate should I use to capitalize leases?",
             "The incremental borrowing rate — what the company itself would pay to borrow over a "
             "similar term. It is often disclosed in the lease note. A market-wide or risk-free "
             "rate understates the liability for weaker credits."),
            ("Does IFRS 16 mean lease capitalization is no longer needed?",
             "Not entirely. IFRS 16 and Ind AS 116 brought most leases on balance sheet, but you "
             "still need the calculation to compare across periods that straddle adoption, across "
             "reporting frameworks, and where disclosure practice differs."),
            ("Why include reverse factoring in debt?",
             "Because it is borrowing. Supply-chain finance is often presented within trade payables, "
             "which improves reported net debt and operating cash flow without reducing the "
             "obligation. Where the exposure is disclosed it belongs in the debt figure."),
            ("What are owner earnings?",
             "Buffett's 1986 definition: net income plus depreciation and amortisation, less the "
             "maintenance capital expenditure required to hold competitive position. This model also "
             "reverses capitalised R&amp;D, adding back its amortisation and expensing the period's "
             "actual cash R&amp;D instead."),
        ],
        "related": ["reverse-dcf-calculator", "black-scholes-calculator",
                    "monte-carlo-stock-price-simulator"],
        "refs": [
            "IASB / ICAI. <em>Ind AS 116 / IFRS 16, Leases</em> (2016).",
            "IASB. <em>Amendments to IAS 7 and IFRS 7: Supplier Finance Arrangements</em> (2023).",
            "Damodaran, A. <em>Dealing with Operating Leases in Valuation.</em> NYU Stern.",
            "Buffett, W. E. (1986). <em>Berkshire Hathaway Shareholder Letter</em> — owner earnings.",
        ],
    },
    {
        "slug": "monte-carlo-stock-price-simulator",
        "what_is_heading": "What a Monte Carlo simulation does",
        "mnemonic": "MC",
        "plan": "free",
        "title": "Monte Carlo Stock Price Simulator — Option Pricing",
        "description": (
            "Free Monte Carlo simulator for stock prices and option values under geometric Brownian "
            "motion, with antithetic variates and standard-error convergence diagnostics."
        ),
        "eyebrow": "Simulation",
        "h1": "Monte Carlo Stock Price Simulator",
        "lede": (
            "Simulate thousands of geometric Brownian motion price paths in your browser and price "
            "an option from the resulting distribution — with the standard error reported, so you "
            "can see how converged the answer actually is."
        ),
        "what_is": [
            "Monte Carlo pricing replaces a closed-form formula with brute force: simulate a very "
            "large number of possible future price paths, compute the option's payoff along each one, "
            "average those payoffs, and discount the average back to today.",
            "Its value is not in pricing vanilla European options — Black-Scholes does that exactly "
            "and instantly. It is in everything that has no closed form: path-dependent payoffs, "
            "early exercise, multiple underlyings, or any situation where you want the whole "
            "distribution of outcomes rather than a single expected value.",
            "It is also the most honest pricing method, because it reports its own uncertainty. A "
            "Monte Carlo price comes with a standard error; a closed-form price does not tell you "
            "anything about whether its assumptions hold.",
        ],
        "formula": (
            "Geometric Brownian motion, exact solution at maturity:\n"
            "\n"
            "  S_T  =  S₀ · exp[ (r − ½σ²)T  +  σ√T · Z ],      Z ~ N(0, 1)\n"
            "\n"
            "Price the option as the discounted mean payoff:\n"
            "\n"
            "  Ĉ  =  e^(−rT) · mean[ max(S_T − K, 0) ]"
        ),
        "vars": [
            ("S₀", "spot price today"),
            ("K", "strike price"),
            ("r", "risk-free rate"),
            ("σ", "volatility"),
            ("T", "time to maturity, in years"),
            ("Z", "a standard normal draw — one per simulated path"),
        ],
        "method": [
            "This simulator uses <strong>antithetic variates</strong>: every random draw Z is paired "
            "with its mirror image −Z. The two paths are negatively correlated, so their errors "
            "partially cancel and the estimator's variance falls without needing more simulations. "
            "It is the cheapest variance reduction available and there is no reason not to use it.",
            "Because the exact solution to geometric Brownian motion is known, the terminal price is "
            "drawn in a single step rather than by stepping through time. That is both faster and "
            "free of discretisation error for payoffs that only depend on the final price.",
            "Every run reports its <strong>standard error</strong>. Monte Carlo error shrinks with "
            "the square root of the number of simulations, so quadrupling the paths halves the "
            "error — worth knowing before you wait on a very large run.",
        ],
        "steps": [
            "Enter spot, strike, risk-free rate, volatility and time to maturity.",
            "Choose the number of simulations. More paths means a tighter standard error, at a "
            "roughly linear cost in time.",
            "Run it, and read the estimated price together with its standard error.",
            "Compare against the analytical Black-Scholes price for a European option — they should "
            "agree within a few standard errors. If they do not, an input is wrong.",
            "Inspect the terminal price distribution, not just the mean. The shape is the part a "
            "closed-form price throws away.",
        ],
        "example_intro": (
            "The repository's own convergence benchmark: a European call with spot 42, strike 40, a "
            "10% risk-free rate, 20% volatility and six months to maturity, run over 400,000 "
            "simulations with a fixed seed."
        ),
        "example_rows": [
            ("Spot (S₀)", "42.00"),
            ("Strike (K)", "40.00"),
            ("Risk-free rate (r)", "10%"),
            ("Volatility (σ)", "20%"),
            ("Maturity (T)", "0.5 years"),
            ("Simulations", "400,000"),
            ("Analytical Black-Scholes price", "4.759"),
        ],
        "example_outro": (
            "The analytical target is Hull's Example 15.6, and the simulation is asserted in this "
            "project's test suite to land within three standard errors of it. That is the right way "
            "to test a Monte Carlo estimator: not against a fixed expected value, which would be "
            "flaky, but against its own convergence guarantee."
        ),
        "assumptions": [
            "Geometric Brownian motion assumes constant volatility and log-normal returns. Real "
            "markets have fat tails and volatility clustering — for the volatility smile, use the "
            "Heston stochastic-volatility model instead.",
            "The simulation is risk-neutral: the drift is the risk-free rate, not an expected "
            "return. Simulated paths are a pricing device, not a forecast of where the stock will go.",
            "The standard error measures simulation noise only. It says nothing about whether the "
            "model itself is right — a precisely converged answer to the wrong model is still wrong.",
            "Results are reproducible only when a seed is fixed. Without one, two runs will differ.",
        ],
        "faq": [
            ("What is a Monte Carlo simulation for stock prices?",
             "It generates a large number of possible future price paths from a stochastic model — "
             "usually geometric Brownian motion — and uses the distribution of outcomes to value a "
             "derivative or assess risk, instead of relying on a closed-form formula."),
            ("How many simulations do I need?",
             "Monte Carlo error falls with the square root of the path count, so quadrupling the "
             "simulations halves the error. Tens of thousands is usually enough for a rough price; "
             "hundreds of thousands for a tight one. Watch the reported standard error rather than "
             "picking a number by feel."),
            ("What are antithetic variates?",
             "A variance-reduction technique that pairs each random draw with its negative. The "
             "paired paths are negatively correlated, so their errors partly cancel and the estimate "
             "gets more accurate for the same number of draws."),
            ("Why does Monte Carlo disagree with Black-Scholes?",
             "For a European option it should not, beyond simulation noise — they price the same "
             "thing. If the gap is much larger than a few standard errors, the inputs differ or the "
             "payoff is not the one Black-Scholes assumes."),
            ("Is this simulator free?",
             "Yes. The Monte Carlo model is one of the ten models available on the free tier, with "
             "no signup required to run it."),
        ],
        "related": ["black-scholes-calculator", "reverse-dcf-calculator",
                    "ifrs-16-hidden-debt-calculator"],
        "refs": [
            "Boyle, P. (1977). <em>Options: A Monte Carlo Approach.</em> Journal of Financial Economics.",
            "Hull, J. (2018). <em>Options, Futures, and Other Derivatives</em>, 10th ed., Example 15.6.",
        ],
    },
    {
        "slug": "black-scholes-calculator",
        "what_is_heading": "What the Black-Scholes model does",
        "mnemonic": "BSM",
        "plan": "free",
        "title": "Black-Scholes Calculator with Greeks — Free, In-Browser",
        "description": (
            "Free Black-Scholes calculator for European calls and puts with all five Greeks — "
            "delta, gamma, vega, theta, rho — validated against Hull's textbook values."
        ),
        "eyebrow": "Derivatives",
        "h1": "Black-Scholes Calculator with Greeks",
        "lede": (
            "Price European calls and puts and get the complete set of Greeks, computed by real "
            "SciPy running in your browser. Benchmarked against Hull's worked example and verified "
            "to satisfy put-call parity to machine precision."
        ),
        "what_is": [
            "The Black-Scholes-Merton model gives a closed-form value for a European option under a "
            "specific set of assumptions: the underlying follows geometric Brownian motion with "
            "constant volatility, there are no transaction costs, and the option can only be "
            "exercised at expiry.",
            "It remains the industry baseline half a century on — not because those assumptions "
            "hold, but because everyone knows exactly how they fail. Implied volatility, the number "
            "the market actually quotes, is defined as the volatility that makes this formula return "
            "the observed price.",
            "The <strong>Greeks</strong> are the partial derivatives of that value with respect to "
            "each input, and they are what the formula is mostly used for in practice: they tell you "
            "how the position's value moves when the world does.",
        ],
        "formula": (
            "  C  =  S·N(d₁)  −  K·e^(−rT)·N(d₂)\n"
            "  P  =  K·e^(−rT)·N(−d₂)  −  S·N(−d₁)\n"
            "\n"
            "         ln(S/K) + (r + ½σ²)T\n"
            "  d₁  =  ─────────────────────  ,      d₂  =  d₁ − σ√T\n"
            "                 σ√T"
        ),
        "vars": [
            ("S", "spot price of the underlying"),
            ("K", "strike price"),
            ("r", "risk-free interest rate"),
            ("σ", "volatility of the underlying"),
            ("T", "time to expiry, in years"),
            ("N(·)", "the standard normal cumulative distribution function"),
        ],
        "method": [
            "The price and every Greek are computed in closed form using SciPy's normal "
            "distribution functions — no finite-difference approximation, so the Greeks are exact "
            "rather than estimated from nearby repricings.",
            "The implementation is validated two ways in this project's test suite. It reproduces "
            "Hull's Example 15.6 to a relative error of 8.9 × 10⁻⁵, and it satisfies "
            "<strong>put-call parity</strong> — C − P = S − K·e^(−rT) — to 4.5 × 10⁻¹⁶. The second "
            "check is the stronger one: parity is an arbitrage identity that must hold exactly, "
            "independent of any particular textbook value.",
            "The whole calculation runs as real CPython in your browser through WebAssembly. "
            "Nothing you type is transmitted anywhere.",
        ],
        "greeks": [
            ("Delta", "∂V/∂S", "How much the option value moves per unit move in the underlying — "
                                "and the hedge ratio."),
            ("Gamma", "∂²V/∂S²", "How fast delta itself changes. High gamma means a hedge goes stale "
                                  "quickly."),
            ("Vega", "∂V/∂σ", "Sensitivity to volatility. Usually the dominant risk on a long option."),
            ("Theta", "∂V/∂T", "Time decay — what the position loses simply by another day passing."),
            ("Rho", "∂V/∂r", "Sensitivity to interest rates. Matters most on long-dated options."),
        ],
        "steps": [
            "Enter spot, strike, risk-free rate, volatility and time to expiry in years.",
            "Choose call or put.",
            "Read the price and the full set of Greeks together — the Greeks are usually the point.",
            "Check put-call parity if you want to verify an input: C − P should equal S − K·e^(−rT).",
            "Cross-check against the Monte Carlo simulator; for a European option the two should "
            "agree within simulation error.",
        ],
        "example_intro": (
            "Hull's Example 15.6, which this implementation is benchmarked against: a European call "
            "with six months to expiry."
        ),
        "example_rows": [
            ("Spot (S)", "42.00"),
            ("Strike (K)", "40.00"),
            ("Risk-free rate (r)", "10%"),
            ("Volatility (σ)", "20%"),
            ("Time to expiry (T)", "0.5 years"),
            ("Call value", "4.759422"),
        ],
        "example_outro": (
            "This reproduces the textbook value to a relative error of 8.9 × 10⁻⁵, and put-call "
            "parity holds to 4.5 × 10⁻¹⁶ — effectively machine precision. Both checks run as part of "
            "the project's automated test suite rather than being asserted by hand."
        ),
        "assumptions": [
            "European exercise only. For American options, which can be exercised early, use the "
            "binomial (Cox-Ross-Rubinstein) model instead.",
            "Volatility is assumed constant across strikes and maturities. Real option markets show "
            "a volatility smile, which is precisely what the Heston model exists to reproduce.",
            "No dividends in the basic form. A continuous dividend yield can be handled by "
            "substituting the dividend-adjusted spot.",
            "Returns are assumed log-normal. Real markets have fatter tails, so deep out-of-the-money "
            "options tend to be underpriced by this formula.",
        ],
        "faq": [
            ("What is the Black-Scholes formula?",
             "A closed-form solution for the value of a European option: C = S·N(d₁) − K·e^(−rT)·N(d₂) "
             "for a call, where d₁ and d₂ are functions of spot, strike, rate, volatility and time."),
            ("What are the Greeks in options?",
             "The sensitivities of an option's value to its inputs: delta to the underlying price, "
             "gamma to delta itself, vega to volatility, theta to the passage of time, and rho to "
             "interest rates."),
            ("Can Black-Scholes price American options?",
             "No. It assumes exercise only at expiry. American options can be exercised early, which "
             "makes them worth at least as much; use a binomial tree for those."),
            ("Why does Black-Scholes disagree with market prices?",
             "Mostly because it assumes constant volatility. Markets price a volatility smile, "
             "charging more for far out-of-the-money strikes than a single volatility implies. That "
             "gap is the reason stochastic-volatility models like Heston exist."),
            ("Is this Black-Scholes calculator free?",
             "Yes. Black-Scholes is one of the ten models on the free tier and runs entirely in your "
             "browser — no signup, and nothing you enter is sent to a server."),
        ],
        "related": ["monte-carlo-stock-price-simulator", "reverse-dcf-calculator",
                    "ifrs-16-hidden-debt-calculator"],
        "refs": [
            "Black, F. &amp; Scholes, M. (1973). <em>The Pricing of Options and Corporate "
            "Liabilities.</em> Journal of Political Economy.",
            "Merton, R. C. (1973). <em>Theory of Rational Option Pricing.</em> Bell Journal of Economics.",
            "Hull, J. (2018). <em>Options, Futures, and Other Derivatives</em>, 10th ed.",
        ],
    },
]

BY_SLUG = {p["slug"]: p for p in PAGES}

PLAN_NOTE = {
    "free": None,
    "pro": ('This is an <strong>Analyst Pro</strong> model ($29/month). While checkout is being set up it is '
            'open to every signed-in account. The other ten models — including Black-Scholes, Monte Carlo and '
            'DCF — are free to run with no signup. <a href="/login">Create an account</a> to get started.'),
}


def _esc(s: str) -> str:
    """Escape a value destined for an HTML attribute."""
    return html.escape(s, quote=True)


def _faq_schema(page: dict) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": _strip_tags(a)},
            }
            for q, a in page["faq"]
        ],
    }


def _strip_tags(s: str) -> str:
    """Schema.org answer text is plain text — drop the inline markup."""
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return html.unescape("".join(out)).strip()


def _app_schema(page: dict) -> dict:
    """SoftwareApplication for this specific calculator.

    `offers` carries the REAL entitlement (see PLAN_NOTE): the free models say
    0, the Analyst Pro ones say 29. Nothing here claims a rating — there is no
    review corpus, and inventing one risks a manual action.
    """
    price = "0" if page["plan"] == "free" else "29"
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": _strip_tags(page["h1"]),
        "url": f"{SITE}/{page['slug']}",
        "applicationCategory": "FinanceApplication",
        "operatingSystem": "Any modern web browser",
        "browserRequirements": "Requires JavaScript and WebAssembly",
        "description": page["description"],
        "isPartOf": {"@id": f"{SITE}/#website"},
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "USD"},
    }


def _breadcrumb_schema(page: dict) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE}/"},
            {"@type": "ListItem", "position": 2, "name": "Calculators",
             "item": f"{SITE}/about"},
            {"@type": "ListItem", "position": 3, "name": _strip_tags(page["h1"]),
             "item": f"{SITE}/{page['slug']}"},
        ],
    }


def _ld(obj: dict) -> str:
    return ('<script type="application/ld+json">\n'
            + json.dumps(obj, indent=2, ensure_ascii=False)
            + "\n</script>")


def render(page: dict) -> str:
    slug, mn = page["slug"], page["mnemonic"]
    url = f"{SITE}/{slug}"
    plan_note = PLAN_NOTE[page["plan"]]
    title_plain = _strip_tags(page["h1"])

    what_is = "\n".join(f"    <p>{p}</p>" for p in page["what_is"])
    method = "\n".join(f"    <p>{p}</p>" for p in page["method"])
    vars_rows = "".join(
        f"<li><code>{v}</code> — {d}</li>" for v, d in page["vars"])
    steps = "\n".join(f"      <li>{s}</li>" for s in page["steps"])
    ex_rows = "\n".join(
        f'        <tr><td>{k}</td><td class="num">{v}</td></tr>'
        for k, v in page["example_rows"])
    assumptions = "\n".join(f"      <li>{a}</li>" for a in page["assumptions"])
    faq = "\n".join(
        f"      <details>\n        <summary>{q}</summary>\n        <p>{a}</p>\n      </details>"
        for q, a in page["faq"])
    refs = "\n".join(f"      <li>{r}</li>" for r in page["refs"])
    related = "\n".join(
        f'      <a href="/{s}"><b>{_strip_tags(BY_SLUG[s]["h1"])}</b>'
        f'<span>{BY_SLUG[s]["eyebrow"]}</span></a>'
        for s in page["related"])

    # Optional per-page blocks
    greeks = ""
    if page.get("greeks"):
        rows = "\n".join(
            f'        <tr><td><strong>{n}</strong></td><td class="num">{sym}</td><td>{d}</td></tr>'
            for n, sym, d in page["greeks"])
        greeks = f"""
    <h3>The Greeks</h3>
    <div class="tablewrap">
      <table>
        <thead><tr><th>Greek</th><th>Derivative</th><th>What it tells you</th></tr></thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </div>"""

    owner = ""
    if page.get("owner_earnings"):
        owner = f"""
    <h3>Owner earnings</h3>
    <div class="formula">{page['owner_earnings']}</div>"""

    plan_html = f'    <p class="plan-note">{plan_note}</p>\n' if plan_note else ""
    cta_note = ("Runs free in your browser — no signup, nothing uploaded."
                if page["plan"] == "free"
                else "Opens the live terminal with this model selected.")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0f1420" />
<title>{_esc(page['title'])}</title>
<meta name="description" content="{_esc(page['description'])}" />
<link rel="canonical" href="{url}" />

<meta property="og:type" content="website" />
<meta property="og:site_name" content="FINMODELS TERMINAL" />
<meta property="og:title" content="{_esc(page['title'])}" />
<meta property="og:description" content="{_esc(page['description'])}" />
<meta property="og:url" content="{url}" />
<meta property="og:image" content="{SITE}/og.png" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta property="og:image:alt" content="FINMODELS TERMINAL — an amber-on-black browser terminal running twelve quantitative-finance models." />
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="{_esc(page['title'])}" />
<meta name="twitter:description" content="{_esc(page['description'])}" />
<meta name="twitter:image" content="{SITE}/og.png" />

<link rel="icon" href="/favicon.ico" sizes="32x32" />
<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
<link rel="apple-touch-icon" href="/apple-touch-icon.png" />
<link rel="stylesheet" href="/assets/landing.css" />

{_ld(_app_schema(page))}
{_ld(_breadcrumb_schema(page))}
{_ld(_faq_schema(page))}
</head>
<body>
<div class="wrap">

  <nav class="crumbs" aria-label="Breadcrumb">
    <a href="/">Terminal</a><span>/</span><a href="/about">Models</a><span>/</span>{title_plain}
  </nav>

  <header>
    <span class="eyebrow">{page['eyebrow']}</span>
    <h1>{page['h1']}</h1>
    <p class="lede">{page['lede']}</p>

    <a class="cta" href="/?m={mn}">
      <span class="go">Open the {title_plain} &rarr;</span>
      <span class="note">{cta_note}</span>
    </a>
{plan_html}  </header>

  <main>
    <h2>{page['what_is_heading']}</h2>
{what_is}

    <h2>The formula</h2>
    <div class="formula">{page['formula']}</div>
    <ul class="vars">{vars_rows}</ul>
{greeks}{owner}

    <h2>How this calculator works</h2>
{method}

    <h2>How to use it</h2>
    <ol>
{steps}
    </ol>

    <h2>Worked example</h2>
    <p>{page['example_intro']}</p>
    <div class="tablewrap">
      <table>
        <thead><tr><th>Input</th><th>Value</th></tr></thead>
        <tbody>
{ex_rows}
        </tbody>
      </table>
    </div>
    <p>{page['example_outro']}</p>

    <h2>Assumptions and limitations</h2>
    <ul>
{assumptions}
    </ul>

    <h2>Frequently asked questions</h2>
    <div class="faq">
{faq}
    </div>

    <h2>Related calculators</h2>
    <div class="related">
{related}
    </div>

    <h2>References</h2>
    <ul>
{refs}
    </ul>
  </main>

  <footer>
    <p>
      Part of <a href="/">FINMODELS TERMINAL</a> — twelve canonical quantitative-finance
      models running on real CPython compiled to WebAssembly, entirely in your browser.
      See <a href="/about">all twelve models</a>.
    </p>
    <p class="updated">Last updated {LASTMOD}.</p>
  </footer>

</div>
</body>
</html>
"""


def render_sitemap() -> str:
    """Emit the whole sitemap here so a new landing page can't be forgotten."""
    entries = []
    for loc, freq, pri, lastmod in STATIC_PAGES:
        entries.append((f"{SITE}{loc}", lastmod, freq, pri))
    for p in PAGES:
        entries.append((f"{SITE}/{p['slug']}", LASTMOD, "monthly", "0.8"))

    body = "\n".join(
        f"  <url>\n"
        f"    <loc>{loc}</loc>\n"
        f"    <lastmod>{lastmod}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{pri}</priority>\n"
        f"  </url>"
        for loc, lastmod, freq, pri in entries)

    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!-- Generated by scripts/build_landing_pages.py — do not hand-edit.\n"
            "     The operator console and the serverless API are deliberately absent\n"
            "     and must stay absent. -->\n"
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{body}\n"
            "</urlset>\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify the committed files match; write nothing")
    args = ap.parse_args()

    outputs = {PUBLIC / f"{p['slug']}.html": render(p) for p in PAGES}
    outputs[PUBLIC / "sitemap.xml"] = render_sitemap()

    stale = []
    for path, content in outputs.items():
        current = path.read_text() if path.exists() else None
        if current == content:
            continue
        if args.check:
            stale.append(path.name)
        else:
            path.write_text(content)
            print(f"wrote {path.relative_to(PUBLIC.parent)}")

    if args.check:
        if stale:
            print("STALE (re-run scripts/build_landing_pages.py): " + ", ".join(stale))
            return 1
        print(f"{len(outputs)} generated files are up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
