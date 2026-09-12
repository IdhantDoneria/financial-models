# MCP endpoint

FINMODELS TERMINAL exposes eleven of its twelve quantitative-finance models as
[Model Context Protocol](https://modelcontextprotocol.io) tools, so an AI client
can run them directly instead of a person driving the sliders in a browser. It is
the same model code — `api/mcp.py` imports the classes from `src/` unchanged, the
same ones the browser runs under Pyodide and the same ones `api/premium.py` uses.

**Endpoint:** `https://financial-models-six.vercel.app/api/mcp`

> This is a modelling and teaching tool, not investment advice. The tools compute
> from the parameters you pass them — they do **not** fetch live market data, so
> every price, growth rate and volatility has to come from you or your user.

## Adding it to a client

**Claude Code** — the nine free models need no credential:

```bash
claude mcp add --transport http finmodels https://financial-models-six.vercel.app/api/mcp
```

To also unlock the two paid models, pass a session token from a signed-in
ANALYST PRO account:

```bash
claude mcp add --transport http finmodels https://financial-models-six.vercel.app/api/mcp --header "Authorization: Bearer YOUR_SESSION_TOKEN"
```

**Claude (web or desktop)** — Settings → Connectors → Add → *Add custom
connector*, then paste the endpoint URL. Because the free tools need no
authentication, the connector works straight after adding it, with no OAuth step.

**Anything else** — any MCP client that speaks the Streamable HTTP transport
works; point it at the same URL.

## The tools

| Tool | Mnemonic | Computes | Paid |
|---|---|---|---|
| `finmodels_list_models` | — | Every model, its formula, parameters and plan requirement | no |
| `finmodels_dcf` | DCF | Enterprise value, equity value and value per share from a projected FCF path | no |
| `finmodels_gordon_growth` | GG | Intrinsic price as a constantly growing dividend perpetuity | no |
| `finmodels_mpt` | MPT | Efficient frontier, min-variance and max-Sharpe tangency portfolios with weights | no |
| `finmodels_var_cvar` | VAR | Value at Risk and expected shortfall (historical / parametric / Monte Carlo) | no |
| `finmodels_capm` | CAPM | Expected return from systematic risk | no |
| `finmodels_black_scholes` | BSM | European option price and the full set of Greeks | no |
| `finmodels_binomial` | CRR | European **or American** option price on a recombining lattice | no |
| `finmodels_monte_carlo` | MC | Simulated option price with antithetic variates and a standard error | no |
| `finmodels_heston` | HES | Option price under stochastic volatility (reproduces the smile) | no |
| `finmodels_hidden_debt` | HDEBT | Ind AS 116 normalisation: leases, reverse factoring, contingent liabilities | **yes** |
| `finmodels_reverse_dcf` | RDCF | The FCF growth rate the market price already implies | **yes** |

### Fama-French is not available here

FF3 is the one model of the twelve that cannot run on this endpoint, and it is
named explicitly in the server instructions and in `finmodels_list_models` rather
than quietly missing from the list.

`FamaFrenchModel.__init__` does an unconditional `import pandas as pd`, and its
factor history is a DataFrame. pandas is the exact dependency that does not fit
this project's serverless Python bundle: `requirements.txt` records ~259 MB
measured against a real 225 MB limit once pandas joins numpy and scipy. Adding it
would take `api/premium.py` down with it.

Run FF3 in the browser terminal at <https://financial-models-six.vercel.app>
(mnemonic `FF3`), where the whole scientific Python stack is already loaded via
WebAssembly.

## Units, defaults, and not lying to you

**Units.** Every rate, growth rate and volatility is a **decimal fraction**, not a
percentage — `0.08` means 8%. Monetary parameters are **in millions** unless the
parameter description says otherwise. Getting this wrong is common enough that
an out-of-range value which looks like a percentage gets an explicit hint back.

**Defaults.** Every parameter is optional and falls back to the same default the
browser's slider starts at. That makes the tools pleasant to call, and it creates
the one failure mode this endpoint genuinely has to defend against: a valuation
that looks real but rests on assumptions the caller never made and cannot see.
This project has shipped that bug once already — a fabricated price/share-count
placeholder once produced a `$54,280,899,174.28` DCF headline (commit `49b49a3`).

So **every result reports `defaulted_inputs`**, listing exactly which parameters
the caller did not supply, and repeats the warning in the human-readable text.
Treat any conclusion that rests on a defaulted input as an assumption, not a
finding. `inputs_used` gives the full set of values the model actually ran on.

Out-of-range values are returned as tool errors naming the real bound, so the
model can correct itself — they are never silently clamped.

## Authentication

The nine free models need no credential at all.

`finmodels_hidden_debt` and `finmodels_reverse_dcf` require an ANALYST PRO (or
higher) plan and an `Authorization: Bearer <session token>` header. The token is a
session token from a signed-in account on the site; entitlement is re-derived
from Redis on every call and never trusted from anything the caller sends.

**This endpoint deliberately ignores the `fm_sess` cookie**, even though
`api/premium.py` honours it. `api/premium.py` is same-origin, called by our own
page. An MCP endpoint is cross-origin by nature, so honouring an ambient cookie
here would let any website a signed-in user happens to visit spend that user's
paid entitlement and monthly quota with a scripted cross-origin POST. A bearer
token has to be handed over deliberately, so there is no ambient credential to
steal — which is also why it is safe for this endpoint to accept any `Origin`.

## Worked example

Real request and response, captured from a local run of the deployed handler:

```bash
curl -X POST https://financial-models-six.vercel.app/api/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: finmodels_black_scholes' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "finmodels_black_scholes",
      "arguments": {"spot": 100, "strike": 95, "sigma": 0.25, "maturity": 2, "rate": 0.04},
      "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    }
  }'
```

```jsonc
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{ "type": "text", "text": "Black-Scholes-Merton (BSM)\nHeadline: $20.21\n..." }],
    "structuredContent": {
      "model": "BSM",
      "headline": "$20.21",
      "results": {
        "price": 20.207161527347516,
        "d1": 0.5481302104396714,
        "d2": 0.19457681984639758,
        "option_type": "call",
        "delta": 0.7081987509157512,
        "gamma": 0.009709880417500696,
        "vega": 48.54940208750349,
        "theta": -5.058846173038072,
        "rho": 101.22542712845522
      },
      "inputs_used": {
        "spot": 100.0, "strike": 95.0, "rate": 0.04, "sigma": 0.25,
        "maturity": 2.0, "dividend_yield": 0.0, "option_type": "call"
      },
      "defaulted_inputs": ["dividend_yield", "option_type"],
      "explanation": "### Black-Scholes-Merton — Call\n\nThe price solves the PDE ..."
    },
    "isError": false,
    "resultType": "complete"
  }
}
```

Note `defaulted_inputs`: the call did not pass `dividend_yield` or `option_type`,
so the result says so. `explanation` is the model's own written derivation, with
LaTeX formulae and a worked example — the same text the terminal's DOC tab shows.

## Protocol notes

The server is **dual-era**, because real clients are currently split across two
incompatible MCP revisions and supporting only one would silently fail for about
half of them:

- **Modern (`2026-07-28`)** — no handshake. Every request carries
  `_meta["io.modelcontextprotocol/protocolVersion"]`, mirrored in the
  `MCP-Protocol-Version` header, alongside `Mcp-Method` and (for `tools/call`)
  `Mcp-Name`. Header and body values must agree or the request is rejected with
  `-32020`. `server/discover` is implemented.
- **Legacy (`2025-11-25`, `2025-06-18`, `2025-03-26`)** — the `initialize`
  handshake, served on the same endpoint with no modern headers required.

Other properties:

- Stateless. No `Mcp-Session-Id`, no resumable streams — modern MCP removed both.
- `POST` only. `GET` and `DELETE` return `405`; `OPTIONS` returns `204` with CORS.
- An unsupported version returns `400` / `-32022` with the supported list.
- An unknown method returns `404` / `-32601`; an unknown *tool* returns `-32602`.
- Bad arguments are **tool errors** (`isError: true`), not JSON-RPC errors, so a
  model can read the message and retry — which it can, since the message names
  the parameter and its real bound.

## Testing

```bash
python3 scripts/test_mcp_api.py                  # 75 protocol/behaviour checks over real HTTP
.venv/bin/python -m pytest tests/test_mcp_schema.py -q   # drift guards
node scripts/dev_mcp_server.js 8787              # run it locally
```

`scripts/test_mcp_api.py` starts the real handler class on a free port and drives
it over HTTP. Beyond protocol conformance it cross-checks five models against the
same classes imported directly from `src/` — that is what proves this endpoint is
plumbing rather than a second implementation that could drift.

`tests/test_mcp_schema.py` guards the two hand-maintained mirrors: `PARAMS` in
`api/mcp.py` against the `MODELS` registry in `public/assets/terminal.js` (so the
UI and the tool schemas cannot disagree about what is a legal input), and the
`_HEADLINE` keys against `SCEN_HEADLINE` in `terminal.js` and `_HEADLINE_PICK` in
`api/premium.py` (so the terminal and an AI client cannot report different
headline numbers for the same run). Six of those headline keys were wrong when
first written, which is why the test exists.
