// GET /api/billing-config — public billing state + plan catalogue.
//
// `billing: false` until RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET (and a store)
// exist, in which case the terminal stays free and unmetered — the PLAN tab
// shows an explicit offline state instead of broken buy buttons.
//
// INTERIM manual-UPI flow for early users — a stopgap so we can take
// payments now WITHOUT the Razorpay integration. This is NOT a replacement
// for Razorpay: the Razorpay code path stays fully intact and is re-enabled
// by setting PAYMENTS_MODE=razorpay (+ the Razorpay keys). Do not delete the
// Razorpay path. `paymentMode` tells the client which checkout UI to render;
// `upi` (only present in upi-manual mode) carries the payee details the
// client needs to build the intent link/QR — the plan catalogue (with
// priceInr) is unchanged and rendered in BOTH modes.

const store = require("../_lib/store");
const B = require("../_lib/billing");

const DEV = process.env.AUTH_DEV_MEMORY === "1";
const PAYMENTS_MODE = process.env.PAYMENTS_MODE === "upi-manual" ? "upi-manual" : "razorpay";
// No .env locally, so give the manual-UPI flow sane defaults under the dev
// harness (AUTH_DEV_MEMORY=1) — otherwise it's untestable without real VPA
// details. In a real deployment these must come from env vars; blank in
// upi-manual mode outside dev means the operator hasn't configured a VPA yet.
const UPI_VPA = process.env.UPI_VPA || (DEV ? "finmodels@upi" : "");
const UPI_NAME = process.env.UPI_NAME || (DEV ? "FINMODELS" : "");
const UPI_NOTE = process.env.UPI_NOTE || "FINMODELS plan";

module.exports = async (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "public, s-maxage=60, stale-while-revalidate=300");
  const billing = B.configured() && store.configured();
  let foundersLeft = null;
  if (store.configured()) {
    try { foundersLeft = await B.foundersLeft(); } catch { /* store hiccup */ }
  }
  res.status(200).json({
    foundersLeft,
    billing,
    mode: B.mode(),
    paymentMode: PAYMENTS_MODE,
    ...(PAYMENTS_MODE === "upi-manual"
      ? { upi: { vpa: UPI_VPA, name: UPI_NAME, note: UPI_NOTE } }
      : {}),
    keyId: billing ? B.keyId() : null,
    devFake: billing && B.mode() === "dev-fake",
    // Actual settlement currency (Razorpay charges INR only). `usdToInr` is
    // the fixed rate `priceUsd` was derived from — the same rate for every
    // plan and every buyer, i.e. no geo discount; it's exposed so the client
    // can show its own USD/INR toggle math consistently if it ever needs to.
    currency: "INR",
    usdToInr: B.USD_TO_INR,
    plans: Object.values(B.PLANS).map((p) => ({
      id: p.id, name: p.name, blurb: p.blurb,
      uploads: p.uploads,                       // null = unlimited
      contact: !!p.contact,                     // sales-led tier (no self-serve checkout)
      seats: p.seats || null,
      // One entry per self-serve billing period; FREE/ENTERPRISE have none.
      // priceInr is what Razorpay actually charges; priceUsd is the same
      // price at the fixed rate above, for display to non-Indian visitors.
      periods: p.periods ? {
        monthly: p.periods.monthly && {
          priceInr: p.periods.monthly.amount / 100, priceUsd: p.periods.monthly.usd,
          days: p.periods.monthly.days },
        annual: p.periods.annual && {
          priceInr: p.periods.annual.amount / 100, priceUsd: p.periods.annual.usd,
          days: p.periods.annual.days },
      } : null,
    })),
    // where the ENTERPRISE "contact sales" button points; override with SALES_EMAIL.
    contactEmail: process.env.SALES_EMAIL || "sales@finmodels.app",
  });
};
