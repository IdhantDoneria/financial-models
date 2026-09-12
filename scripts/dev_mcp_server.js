#!/usr/bin/env node
/* Local harness for api/mcp.py.
 *
 * scripts/dev_auth_server.js only routes api/*.js — it has no Python runtime,
 * so the MCP endpoint would otherwise only ever be exercisable in production.
 * This runs the real handler class out of api/mcp.py through Python's own
 * http.server, which is the same BaseHTTPRequestHandler interface Vercel's
 * Python runtime invokes, so what is tested here is the deployed code path
 * rather than a stand-in.
 *
 * Usage:  node scripts/dev_mcp_server.js [port]     (default 8787)
 */
"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");

const PORT = process.argv[2] || "8787";
const ROOT = path.resolve(__dirname, "..");

const PY = `
import sys, os
sys.path.insert(0, ${JSON.stringify(ROOT)})
os.chdir(${JSON.stringify(ROOT)})
from http.server import ThreadingHTTPServer
from api.mcp import handler
srv = ThreadingHTTPServer(("127.0.0.1", ${Number(PORT)}), handler)
print("mcp dev server: http://127.0.0.1:${Number(PORT)}/api/mcp", flush=True)
srv.serve_forever()
`;

const py = spawn(process.env.PYTHON || "python3", ["-c", PY], {
  cwd: ROOT,
  stdio: "inherit",
});
py.on("exit", (code) => process.exit(code ?? 0));
for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => py.kill(sig));
