/* rfe.api.js
   Thin fetch client between the browser and the Fares-Cockpit FastAPI backend.

   Contract:
   - Every call is async, returns a Promise resolving to the parsed JSON.
   - Every mutating call carries an AbortController that supersedes any
     prior in-flight of the same kind (parameters change fast on a slider).
   - No retries here — the caller decides. HTTP 4xx surfaces as a rejected
     Promise with the parsed body (typed engine statuses come back 200).
   - The current env mode ('demo' | 'live') is forwarded as `X-Fares-Mode`
     so the backend can flip HSP + ticker sources without a duplicate app.
*/
(function () {
  "use strict";
  var envMode = "demo";
  var inflight = {}; // {kind: AbortController}
  var baseHeaders = function () {
    return { "X-Fares-Mode": envMode, "Content-Type": "application/json" };
  };

  function abortPrior(kind) {
    var prior = inflight[kind];
    if (prior && typeof prior.abort === "function") { try { prior.abort(); } catch (e) {} }
    var ctrl = new AbortController();
    inflight[kind] = ctrl;
    return ctrl;
  }

  function jsonOrThrow(res) {
    return res.text().then(function (t) {
      var body;
      try { body = t ? JSON.parse(t) : null; } catch (e) { body = { detail: t }; }
      if (!res.ok) {
        var err = new Error("HTTP " + res.status);
        err.status = res.status;
        err.body = body;
        throw err;
      }
      return body;
    });
  }

  function api_get(path, opts) {
    opts = opts || {};
    var ctrl = opts.kind ? abortPrior(opts.kind) : null;
    return fetch(path, {
      method: "GET",
      headers: baseHeaders(),
      signal: ctrl ? ctrl.signal : undefined,
    }).then(jsonOrThrow);
  }

  function api_post(path, body, opts) {
    opts = opts || {};
    var ctrl = opts.kind ? abortPrior(opts.kind) : null;
    return fetch(path, {
      method: "POST",
      headers: baseHeaders(),
      body: JSON.stringify(body),
      signal: ctrl ? ctrl.signal : undefined,
    }).then(jsonOrThrow);
  }

  window.RFE_API = {
    setEnvMode: function (mode) { envMode = (mode === "live") ? "live" : "demo"; },
    getEnvMode: function () { return envMode; },
    // Readiness probe — short timeout so a warming/GIL-starved backend
    // reads as "not ready yet" instead of a fetch that hangs for minutes.
    health: function (timeoutMs) {
      var ctrl = new AbortController();
      var t = setTimeout(function () { ctrl.abort(); }, timeoutMs || 4000);
      return fetch("/api/health", { headers: baseHeaders(), signal: ctrl.signal })
        .then(jsonOrThrow)
        .finally(function () { clearTimeout(t); });
    },
    // Metadata (called once on boot)
    snapshot: function () { return api_get("/api/snapshot"); },
    corridors: function () { return api_get("/api/corridors"); },
    stations: function () { return api_get("/api/stations"); },
    railcards: function () { return api_get("/api/railcards"); },
    tocs: function () { return api_get("/api/tocs"); },
    route: function (origin, dest) {
      return api_get("/api/route?origin=" + encodeURIComponent(origin) +
                     "&dest=" + encodeURIComponent(dest));
    },
    // Working endpoints
    resolve: function (origin, dest, ticket, opts) {
      opts = opts || {};
      var qs = "?origin=" + origin + "&dest=" + dest + "&ticket=" + ticket;
      if (opts.railcard) qs += "&railcard=" + opts.railcard;
      if (opts.route) qs += "&route=" + opts.route;
      return api_get("/api/resolve" + qs, { kind: "resolve:" + origin + "-" + dest + ":" + ticket + ":" + (opts.railcard || "") });
    },
    impact: function (changeRequest, includes) {
      var inc = (includes && includes.length) ? "?include=" + includes.join(",") : "";
      return api_post("/api/impact" + inc, changeRequest, { kind: "impact" });
    },
    staging: function () { return api_get("/api/staging"); },
    propose: function (changeRequest) {
      return api_post("/api/staging/propose", changeRequest, { kind: "propose" });
    },
    approve: function (cardId) {
      return api_post("/api/staging/" + encodeURIComponent(cardId) + "/approve", null,
        { kind: "approve:" + cardId });
    },
    resetStaging: function () {
      return api_post("/api/staging/reset", null, { kind: "reset" });
    },
  };
})();
