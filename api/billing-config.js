// GET /api/billing-config — public billing state + plan catalogue.
//
// `billing: false` until RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET (and a store)
// exist, in which case the terminal stays free and unmetered — the PLAN tab
// shows an explicit offline state instead of broken buy buttons.

const store = require("./_lib/store");
const B = require("./_lib/billing");

module.exports = (req, res) => {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "public, s-maxage=300, stale-while-revalidate=3600");
  const billing = B.configured() && store.configured();
  res.status(200).json({
    billing,
    mode: B.mode(),
    keyId: billing ? B.keyId() : null,
    devFake: billing && B.mode() === "dev-fake",
    currency: "INR",
    plans: Object.values(B.PLANS).map((p) => ({
      id: p.id, name: p.name, blurb: p.blurb,
      priceInr: p.amount / 100, mrpInr: p.mrp ? p.mrp / 100 : null,
      uploads: p.uploads,                       // null = unlimited
    })),
    validityDays: B.PLAN_TTL / 86_400,
  });
};
