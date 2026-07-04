# FINMODELS — Sales Script Suite (B2B: PMS firms & small hedge funds)

Selling the service: **a Bloomberg-style financial-modeling terminal in the browser** —
ten institutional models (DCF → Heston stochastic volatility), a scenario & sensitivity
engine, 15-market rate anchoring, and a desk that turns any company filing or annual
report PDF into a client-ready multi-model valuation report in minutes.

**Target buyer:** founders/PMs of small portfolio-management services (PMS) and boutique
hedge funds — teams of 2–15 who run **continuous modelling of equity and derivatives
positions** without Bloomberg-scale budgets, and who answer to **serious, demanding
investors** who expect institutional rigor in every review.

**Positioning in one line:**
> *"Your analysts' modelling week, compressed to an afternoon — at 1% of a Bloomberg seat."*

**The three pains we sell against (in the buyer's own words):**
1. **Time bleed** — every mandate review, every vol regime change, every rate move means
   re-running the same DCFs, option books, and VaR numbers by hand in Excel.
2. **Credibility risk** — one wrong cell in front of a serious investor costs more than
   any software ever will. Their capital follows their confidence.
3. **Tooling gap** — Bloomberg/FactSet is priced for institutions ($25k–30k/seat/yr);
   spreadsheets don't scale; hiring another analyst is $80k+ before bonus.

**Psychology principles used throughout** (the product genuinely backs every claim — keep it that way):
- **Anchoring** — name the Bloomberg seat price and the analyst salary *first*; your price lands as a rounding error.
- **Loss aversion** — sell the cost of a wrong number in an investor meeting, not the joy of a feature. Fund managers are professionally terrified of being wrong in public; speak to that.
- **Authority** — Hull, Damodaran, Heston, Fama-French; benchmarks to machine precision; open, auditable code. For an allocator-facing buyer, *auditability is the product*.
- **Concreteness** — "type `HES <GO>`, drag ξ, watch the smile reprice in 15 ms" beats any adjective.
- **Reciprocity** — give before asking: send a free sample analysis of one of *their* holdings.
- **Commitment & consistency** — micro-yes ladder: open the link (guest, no signup) → run one model → book the demo → pilot → license.
- **Scarcity** — "founding-fund" pricing and limited white-glove onboarding slots. *Only if real.*
- **Identity / status** — they aren't buying software; they're buying "my two-person shop reports like a desk at Goldman."

**Compliance guardrail:** never promise returns, alpha, or performance. Sell *speed,
rigor, and presentation* to people who manage other people's money — they will respect
the restraint, and regulators require it.

---

## 1 · Cold email sequence (fund founders / PMs)

### Email 1 — the time-bleed audit (Day 0)
**Subject:** How many analyst-hours did last week's vol move cost you?

Hi {{first_name}},

When rates or vol move, most boutique funds I speak to lose one to two analyst-days
re-marking the book: repricing the options positions, refreshing the equity DCFs,
re-running VaR for the investor letter. Every single time.

That's the work we've automated. FINMODELS is a Bloomberg-style terminal that runs in
the browser — ten institutional models (Black-Scholes, binomial, Monte Carlo, **Heston
stochastic volatility**, DCF, CAPM, VaR/CVaR, Fama-French among them), each validated
against the academic literature to machine precision. Change an assumption, and every
output, tornado chart, and scenario grid reprices live — in milliseconds, not meetings.

Your book, stress-tested before the market opens. No install, no IT, no data contract.

Worth 15 minutes? Reply and I'll set up a walkthrough on *your* positions — or try it
yourself right now as a guest (no card, no signup): {{site_url}}

{{sender_name}}

> **Psychology:** opens with a cost the reader has personally felt (loss aversion +
> vividness), authority via named models, closes with a two-lane micro-commitment.

### Email 2 — the reciprocity strike (Day 3)
**Subject:** I ran {{holding_name}} through ten models — the report's attached

{{first_name}},

No pitch in this one. I took {{holding_name}}'s latest annual report, dropped the PDF
into our terminal, and attached what came out: financials auto-extracted, IB-grade
assumptions filled (CAPM WACC, terminal growth capped at the sovereign risk-free rate,
pulled live), and all ten models run — DCF fair value through option-implied dynamics —
exported to PDF.

Total time: about two minutes. The same report, branded for your fund, is what your
investors could be seeing every review cycle.

If the assumptions the bot chose look wrong to you — good. Every one of them is a
slider. That's the point.

{{sender_name}}

> **Psychology:** reciprocity (real work, delivered free, on THEIR holding) + proof by
> demonstration + a disarming "if it looks wrong, good" that flips skepticism into engagement.

### Email 3 — the credibility frame (Day 7)
**Subject:** What your investors are really auditing

{{first_name}},

Serious LPs don't audit your returns first — they audit your *process*. The question
behind every question is: "if I hand these people money, how rigorous is the machine?"

FINMODELS makes the machine visible: every model cites its source (Hull, Damodaran,
Heston 1993), ships literature benchmarks (Black-Scholes reproduces Hull's Example 15.6
to a relative error of 8.9e-05; put-call parity to 4.5e-16), and produces scenario
tables — bear/base/bull, tornado charts, two-way sensitivity grids — where **every cell
is a real model run**, not a linearisation. Rates anchor to any of the 15 largest
markets, so a Mumbai or Singapore book is priced off its own sovereign curve, not a US
default.

That's the difference between telling an investor your number and *showing them why it
survives stress*.

15-minute walkthrough this week? Two slots: {{slot_a}} or {{slot_b}}.

{{sender_name}}

> **Psychology:** reframes the purchase as investor-facing credibility (status + fear of
> looking unrigorous), authority via specifics, alternative close (two slots, not yes/no).

### Email 4 — breakup + honest scarcity (Day 12)
**Subject:** Closing the file on {{fund_name}}

{{first_name}},

Last note from me. We onboard a limited number of funds per month — white-glove setup
on your actual book is hands-on work — and **founding-fund pricing ({{price}}, locked
for life) ends {{date}} / is capped at the first {{N}} funds**. After that it's
{{standard_price}}.

If continuous, desk-grade modelling isn't a priority this quarter, no hard feelings —
the guest tier stays free: {{site_url}}

{{sender_name}}

> **Psychology:** takeaway close (withdrawing pursuit raises perceived value) + capacity-based
> scarcity, which is credible for white-glove B2B because it is genuinely true. **Only
> state the cap and deadline if you will honor them.**

---

## 2 · LinkedIn DM script (fund founders, PMs, heads of research)

**Opener (personalized, no pitch):**
> Saw {{fund_name}}'s note on {{topic — e.g., vol positioning / mid-cap valuations}} —
> curious, when the regime shifts like this, how long does it take your team to re-mark
> the whole book? Genuinely asking, it's the problem I work on.

**Bridge (after any reply — ask permission):**
> That matches what I hear from most sub-15-person funds. We built a browser terminal
> that reprices all of it live — Heston/BSM/Monte Carlo for the derivatives side, DCF
> through VaR for equity — validated to machine precision, exports investor-ready
> reports. Want the link? Guest mode is free, no signup.

**Close (after they try it):**
> What did you run first? … The paid tier adds saved analyses per account, your fund's
> markets pinned, and the full filing-to-report desk. We're pricing founding funds at
> {{price}} — want me to hold a slot while you kick the tires?

---

## 3 · Demo / discovery call script (15 minutes, founder-to-founder)

**1 · Frame (30s):**
"Thanks for the time — 15 minutes, here's the shape: two minutes on where modelling
time goes at funds your size, ten minutes live on the terminal — *you* drive, ideally
on one of your names — and if it's a fit, how a fund license works. Fair?"
*(Agreeing to the agenda = first yes.)*

**2 · Pain discovery (ask, don't tell — make THEM say the numbers):**
- "Walk me through what happens at your shop when implied vol gaps 5 points — who
  re-runs what, and how long does it take?"
- "How do you currently show an investor *why* your valuation survives a rate shock?"
- "What's a fully-loaded analyst-hour cost you?" *(They anchor the ROI themselves.)*
- "Have you priced a Bloomberg or FactSet seat lately?" *(They say $25k, not you.)*

**3 · Demo (let them drive — what they operate, they value):**
- Have *them* type `HES <GO>` and drag vol-of-vol. "That's real Python — the same
  files an 88-test suite validates — repricing in about 15 milliseconds."
- Have them upload one of their holdings' annual reports on the IB desk. Then be quiet
  while it extracts, assumes, and runs ten models. Let the silence do the selling.
- Open the SCEN tab on their number: "Bear/base/bull, tornado, two-way grid — every
  cell a real run. This is the page you put in front of an investor."
- Flip the market selector to their domicile: "Your risk-free rate, your sovereign
  curve — not a US default."

**4 · Objection handling:**
| Objection | Response |
|---|---|
| "We have our own Excel models." | "Keep them — this isn't replacing your house view, it's the engine that stress-tests it continuously. Excel gives you the number once; this tells you every morning whether it still holds." |
| "Is it rigorous enough for our investors?" | "It's more auditable than anything closed-source: every model cites its literature source and ships benchmarks — Black-Scholes to Hull at 8.9e-05 relative error, put-call parity at 4.5e-16, Heston collapsing to BSM as ξ→0. Your investors can check the math." |
| "We're not US-focused." | "Neither is the terminal — pick any of the 15 largest markets and every model re-anchors to that sovereign curve and ERP. Filing analysis handles annual and quarterly reports generally, not one country's format." |
| "Too expensive." | "Against what baseline — the $25k seat, the $80k analyst, or the investor who walks after one unstressed number? This costs less than the coffee budget of any of those." |
| "Send me something, we'll think about it." | "Happy to — what's the one thing that would need to be true for this to be a yes? I'll make the follow-up about exactly that." *(Isolate the real objection.)* |

**5 · Close (assumptive, alternative — never yes/no):**
"Founding funds start one of two ways: the team license outright, or a two-week pilot
where we set it up on five of your names and you judge the time saved. Which suits how
you buy?"

---

## 4 · Pricing psychology cheat-sheet (B2B)

- **Anchor stack, in order:** Bloomberg seat ($25k/yr) → junior analyst ($80k+) → your
  fund license ({{price}}). Presented in that sequence, yours reads as free.
- **Per-fund, not per-seat** for teams under ~10 — removes internal friction and makes
  the champion's pitch to partners trivial ("one line item, whole team").
- **ROI framing in their unit:** "If it saves one analyst-day per month, it pays for
  itself {{X}}× over." Let the discovery-call numbers (theirs) fill the formula.
- **Pilot as endowment:** two weeks on their own book. Taking it away after they've
  saved analyses, pinned markets, built scenarios — loss aversion works for you.
- **Founding-fund lock-in:** early price guaranteed for life; converts urgency into loyalty.
- **Round, confident numbers** for institutions ($2,400/yr, not $2,399) — charm pricing
  reads retail to professional buyers.

## 5 · What never to do

- No promised returns, alpha, or "edge" — you're selling to regulated money managers;
  performance claims poison the well and the compliance review.
- No fabricated AUM served, fund counts, or testimonials. Once real funds are aboard,
  ask for referenceable quotes — one true sentence from a peer fund outsells any ad.
- No fake deadlines or evergreen "last slots." Capacity scarcity only when real.
- Persuasion here = making a genuinely strong product's strength *felt* by a skeptical,
  numerate buyer. The product can cash every check this copy writes; keep it that way.
