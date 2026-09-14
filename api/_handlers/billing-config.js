// GET /api/billing-config — public billing state + plan catalogue.
//
// `billing: false` until RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET (and a store)
// exist, in which case the terminal stays free and unmetered — the PLAN tab
// shows an explicit offline state instead of broken buy buttons.

const store = require("../_lib/store");
const B = require("../_lib/billing");

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
