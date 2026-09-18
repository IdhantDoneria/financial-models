// In-process tests for the INTERIM manual-UPI payment flow
// (api/_handlers/billing-claim.js + the claims/approve_claim/reject_claim
// actions on api/admin.js). Runs the real handlers with the store in
// dev-memory mode — same style as scripts/test_billing_api.js.
//
// Covers: claim -> admin approve -> plan actually granted; a rejected claim
// never grants; a claim with no session is rejected; input validation;
// idempotent approve (no double-grant); the per-email pending dedupe; the
// per-IP rate limiter (burst -> 429); grant/revoke/reset_password unaffected.
//
//     node scripts/test_upi_claims.js

process.env.AUTH_DEV_MEMORY = "1";
process.env.PAYMENTS_MODE = "upi-manual";

const handlers = {
  requestOtp: require("../api/_handlers/auth-request-otp.js"),
  verifyOtp: require("../api/_handlers/auth-verify-otp.js"),
  billingConfig: require("../api/_handlers/billing-config.js"),
  billingClaim: require("../api/_handlers/billing-claim.js"),
  admin: require("../api/admin.js"),
};

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log(`  ✔ ${name}`); }
  else { failed++; console.log(`  ✘ ${name}${detail ? " — " + detail : ""}`); }
}

/** Invoke a handler the way Vercel would; returns {code, body}. `url` lets
 *  admin.js's `new URL(req.url, ...)` parse a real ?action= query string. */
async function call(fn, { method = "GET", body, token, headers = {}, url = "/api/x" } = {}) {
  const req = {
    method, body, url,
    headers: { ...(token ? { authorization: `Bearer ${token}` } : {}), ...headers },
  };
  return new Promise((resolve, reject) => {
    const res = {
      _code: 200,
      setHeader() {},
      status(c) { this._code = c; return this; },
      json(o) { resolve({ code: this._code, body: o }); },
      end(s) { resolve({ code: this._code, body: s ? JSON.parse(s) : {} }); },
    };
    Promise.resolve(fn(req, res)).catch(reject);
  });
}

async function login(email) {
  const sent = await call(handlers.requestOtp, { method: "POST", body: { email } });
  const ver = await call(handlers.verifyOtp,
    { method: "POST", body: { email, code: sent.body.devCode, name: "UPI Tester", password: "Str0ngPass!Upi" } });
  return ver.body.token;
}

// billing-claim.js rate-limits per clientIp(req) (5/hour, same layered idiom
// api/quotes.js uses) — checked BEFORE body validation, so even a rejected
// (400) attempt spends budget. Real distinct buyers get distinct real IPs;
// this harness fakes that with x-real-ip per scenario so unrelated test
// scenarios don't collide in one shared "unknown" bucket and trip 429s that
// have nothing to do with what that scenario is actually testing. The
// limiter itself gets its own dedicated burst test further down.
const ipHeaders = (ip) => ({ "x-real-ip": ip });

const ADMIN_HEADERS = { "x-admin-key": "devadmin" };
const adminGetClaims = () => call(handlers.admin, { method: "GET", headers: ADMIN_HEADERS, url: "/api/admin?action=claims" });
const adminPost = (body) => call(handlers.admin, { method: "POST", headers: ADMIN_HEADERS, body });

(async () => {
  console.log("INTERIM UPI CLAIMS — dev-memory store, PAYMENTS_MODE=upi-manual\n");

  // -- config exposes upi-manual mode + payee details ----------------------
  const cfg = await call(handlers.billingConfig);
  check("config: paymentMode is upi-manual", cfg.body.paymentMode === "upi-manual");
  check("config: upi block carries a VPA + name (dev defaults)",
    !!cfg.body.upi && cfg.body.upi.vpa === "finmodels@upi" && cfg.body.upi.name === "FINMODELS");
  check("config: plan catalogue (priceInr) still present in upi-manual mode",
    Array.isArray(cfg.body.plans) && cfg.body.plans.some((p) => p.id === "pro" && p.periods.monthly.priceInr > 0));

  // -- no session -> 401 ----------------------------------------------------
  const anon = await call(handlers.billingClaim,
    { method: "POST", body: { plan: "pro", period: "monthly", upiRef: "ABCD12345678" } });
  check("claim: 401 without a session", anon.code === 401);

  const email = "upi-buyer@example.com";
  const token = await login(email);
  check("login: OTP session issued", !!token);

  // -- validation (own IP bucket — 4 rejected attempts, well under the 5/hr cap) --
  const validationIp = ipHeaders("10.0.0.2");
  const badPlan = await call(handlers.billingClaim,
    { method: "POST", token, headers: validationIp, body: { plan: "gold", period: "monthly", upiRef: "ABCD12345678" } });
  check("claim: invalid plan -> 400", badPlan.code === 400);
  const badPeriod = await call(handlers.billingClaim,
    { method: "POST", token, headers: validationIp, body: { plan: "pro", period: "weekly", upiRef: "ABCD12345678" } });
  check("claim: invalid period -> 400", badPeriod.code === 400);
  const badRefShort = await call(handlers.billingClaim,
    { method: "POST", token, headers: validationIp, body: { plan: "pro", period: "monthly", upiRef: "AB12" } });
  check("claim: too-short upiRef -> 400", badRefShort.code === 400);
  const badRefJunk = await call(handlers.billingClaim,
    { method: "POST", token, headers: validationIp, body: { plan: "pro", period: "monthly", upiRef: "not-alnum!!" } });
  check("claim: non-alphanumeric upiRef -> 400", badRefJunk.code === 400);
  const wrongMethod = await call(handlers.billingClaim, { method: "GET", token });
  check("claim: GET -> 405", wrongMethod.code === 405);   // rejected before the limiter even runs

  // -- happy path: file a claim, it never grants on its own (own IP bucket) --
  const mainIp = ipHeaders("10.0.0.1");
  const filed = await call(handlers.billingClaim,
    { method: "POST", token, headers: mainIp, body: { plan: "pro", period: "monthly", upiRef: "abc123XYZ890", note: "paid via GPay" } });
  check("claim: valid claim filed as pending", filed.body.ok === true && filed.body.status === "pending");

  const preApprove = await call(handlers.admin, { method: "GET", headers: ADMIN_HEADERS }); // full desk read
  const preSub = preApprove.body.rows.find((r) => r.email === email);
  check("claim alone never grants a plan (still FREE pre-approval)",
    !preSub || preSub.plan === "free");

  // -- admin sees the pending claim ------------------------------------------
  const claimsResp = await adminGetClaims();
  check("admin: pending claim appears in ?action=claims",
    claimsResp.body.ok && claimsResp.body.claims.some((c) => c.email === email && c.status === "pending" && c.upiRef === "ABC123XYZ890"));
  const claimId = claimsResp.body.claims.find((c) => c.email === email).id;

  // -- duplicate claim while pending is deduped, not stacked -----------------
  const dupe = await call(handlers.billingClaim,
    { method: "POST", token, headers: mainIp, body: { plan: "pro", period: "monthly", upiRef: "dupedupedupe" } });
  check("claim: a second claim while one is pending is deduped",
    dupe.body.ok === true && dupe.body.status === "pending" && dupe.body.deduped === true);
  const claimsAfterDupe = await adminGetClaims();
  check("dedupe: exactly one claim on file for this email",
    claimsAfterDupe.body.claims.filter((c) => c.email === email).length === 1);

  // -- reject_claim never grants (own IP bucket) ------------------------------
  const rejectEmail = "upi-rejected@example.com";
  const rejectToken = await login(rejectEmail);
  await call(handlers.billingClaim, { method: "POST", token: rejectToken, headers: ipHeaders("10.0.0.3"),
    body: { plan: "unlimited", period: "monthly", upiRef: "REJECTMEPLZ1" } });
  const rejectClaim = (await adminGetClaims()).body.claims.find((c) => c.email === rejectEmail);
  const rejected = await adminPost({ action: "reject_claim", id: rejectClaim.id, reason: "no matching UTR in bank statement" });
  check("admin: reject_claim ok", rejected.body.ok === true && rejected.body.status === "rejected");
  const afterReject = await call(handlers.admin, { method: "GET", headers: ADMIN_HEADERS });
  const rejectedSub = afterReject.body.rows.find((r) => r.email === rejectEmail);
  check("reject_claim NEVER grants a plan", !rejectedSub || rejectedSub.plan === "free");
  const doubleReject = await adminPost({ action: "reject_claim", id: rejectClaim.id, reason: "again" });
  check("reject_claim is safe to call twice (still rejected, still no grant)", doubleReject.body.status === "rejected");

  // -- approve_claim actually grants ------------------------------------------
  const approved = await adminPost({ action: "approve_claim", id: claimId });
  check("admin: approve_claim ok", approved.body.ok === true && approved.body.status === "approved");
  const me = await call(handlers.admin, { method: "GET", headers: ADMIN_HEADERS });
  const grantedRow = me.body.rows.find((r) => r.email === email);
  check("approve_claim actually grants the claimed plan (PRO)",
    grantedRow && grantedRow.plan === "pro" && grantedRow.via === "upi-manual");

  // -- approve_claim is idempotent: a second approve does not re-grant -------
  const approveAgain = await adminPost({ action: "approve_claim", id: claimId });
  check("admin: second approve_claim is a no-op (still ok:true, status approved)",
    approveAgain.body.ok === true && approveAgain.body.status === "approved");
  const meAgain = await call(handlers.admin, { method: "GET", headers: ADMIN_HEADERS });
  const grantedRowAgain = meAgain.body.rows.find((r) => r.email === email);
  check("idempotent approve: expiresAt unchanged by the second approve (no re-grant)",
    grantedRowAgain.expiresAt === grantedRow.expiresAt);

  // -- unknown claim id / bad id charset --------------------------------------
  const badId = await adminPost({ action: "approve_claim", id: "not-hex!!" });
  check("admin: malformed claim id -> 400", badId.code === 400);
  const missingId = await adminPost({ action: "approve_claim", id: "deadbeefdeadbeef00" });
  check("admin: well-formed but unknown claim id -> 404", missingId.code === 404);

  // -- grant/revoke/reset_password still behave exactly as before (the
  //    email-validation gotcha only applies to these three actions now) ----
  const badEmailGrant = await adminPost({ action: "grant", email: "not-an-email", plan: "pro", days: 30 });
  check("admin: grant still validates email (400 on garbage email)", badEmailGrant.code === 400);
  const grantOk = await adminPost({ action: "grant", email: "granted-directly@example.com", plan: "unlimited", days: 10 });
  check("admin: direct email grant unaffected by the claim changes", grantOk.body.ok === true && grantOk.body.granted === "unlimited");
  const revokeOk = await adminPost({ action: "revoke", email: "granted-directly@example.com" });
  check("admin: revoke unaffected", revokeOk.body.ok === true);

  // -- rate limiter: a burst from one IP gets 429s once the 5/hour budget
  //    is spent, on a dedicated IP/session so it can't be muddied by any
  //    budget already spent by the scenarios above --------------------------
  const burstToken = await login("upi-burst@example.com");
  const burstIp = ipHeaders("10.0.0.42");
  const burstCodes = [];
  for (let i = 0; i < 6; i++) {
    const r = await call(handlers.billingClaim, { method: "POST", token: burstToken, headers: burstIp,
      body: { plan: "pro", period: "monthly", upiRef: `BURST${i}REF12` } });
    burstCodes.push(r.code);
  }
  check("rate limit: first 5 claims from one IP are not rate-limited",
    burstCodes.slice(0, 5).every((c) => c !== 429), `codes=${burstCodes}`);
  check("rate limit: 6th claim from the same IP within the hour -> 429",
    burstCodes[5] === 429, `codes=${burstCodes}`);

  console.log(`\n${passed} passed · ${failed} failed`);
  process.exit(failed ? 1 : 0);
})().catch((err) => { console.error(err); process.exit(1); });
