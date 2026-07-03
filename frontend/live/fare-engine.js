/* fare-engine.js — REAL BACKEND ADAPTER
   ----------------------------------------------------------------------
   The delivered `class Component extends DCLogic` in index.html expects a
   synchronous `window.RFE` surface (SNAPSHOT / STATIONS / ST / CORRIDORS /
   CLUSTERS / CLUSTER_LABEL / FLOWS + helper functions + a synchronous
   `resolve()` and `computeImpact()`).

   This module preserves that surface exactly, but populates it from real
   backend responses. `computeImpact` and `resolve` are semantically async
   underneath — they return the LAST successful cache immediately (so the
   template renders something), and kick off an AbortController-guarded
   fetch in the background. When the fetch resolves we update the cache
   and ask the delivered Component (captured as `window.__rfeRoot`) to
   re-render — its next call reads the fresh value from the cache.

   Correctness invariants (matching the plan's rule list):
   1) Request signatures keyed on ChangeRequest+includes; late responses
      whose sig no longer matches are discarded silently.
   2) Empty modular blocks (compliance: {breaches: []}) are DIFFERENT from
      absent modular blocks (compliance: null). The adapter preserves this.
   3) State is never mutated in place — every response replaces the cache
      object wholesale.
   4) Coalescing: identical Change signatures within the same session skip
      the fetch entirely.
*/
(function () {
  "use strict";

  var A = window.RFE_API;
  var ADAPT = window.RFE_ADAPT;
  if (!A || !ADAPT) {
    console.error("[rfe] RFE_API or RFE_ADAPT missing; check script order");
    return;
  }

  /* ---- caches (in-memory, cleared on page unload) --------------------- */
  var _snapshot = null;
  var _corridors = [];
  var _stations = [];         // list from /api/stations
  var _stationsByCrs = {};    // {CRS: StationModel}
  var _stationsByNlc = {};    // {NLC: StationModel} for adapter joins
  var _railcards = [];        // list from /api/railcards
  var _tocs = [];             // list from /api/tocs (operator scoping)
  var _lastImpact = null;
  var _lastImpactSig = null;     // signature we WANT (set at request time)
  var _lastImpactDataSig = null; // signature _lastImpact actually corresponds to
  var _lastImpactRaw = null;
  var _resolveCache = new Map();
  var _resolveInflight = new Set();
  var _cstatsCache = new Map();      // "O-D" -> corridor stats payload
  var _cstatsInflight = new Set();
  var _overviewCache = null;         // /api/overview payload
  var _overviewInflight = false;

  /* ---- utilities ------------------------------------------------------ */
  function sigOf(o) { return JSON.stringify(o); }

  function euclideanKm(a, b) {
    // Fallback distance when we can't get one from the routeing engine.
    // Uses SVG-projected coords; the gb-outline projection maps 1 SVG
    // unit to 1/scale ≈ 1872 m of BNG, so 1 unit ≈ 1.87 km. Fine for
    // ordering rows and picking a split mid-point.
    if (!a || !b) return 0;
    var dx = a.x - b.x, dy = a.y - b.y;
    var svgDist = Math.sqrt(dx * dx + dy * dy);
    return Math.round(svgDist * 1.87);
  }

  /* ---- ChangeRequest builder from the Component's `params` ------------ */
  function _feedRailcardCodes() {
    var out = {};
    _railcards.forEach(function (r) { if (r.in_feed) out[r.code] = true; });
    return out;
  }

  function _proposalCode(params) {
    // The backend REJECTS railcard_codes already present in .RLC (a synthetic
    // proposal must not shadow a real railcard — change_request.py). So each
    // cockpit railcard maps to a deterministic synthetic 3-char code:
    //   - explicit params.railcardCode wins (wizard-created cards carry one),
    //   - otherwise "Z" + 2 digits hashed from the railcard name, so the same
    //     card always proposes under the same code (idempotent re-propose)
    //     while different cards get different codes.
    if (params.railcardCode) return params.railcardCode;
    var name = params.railcardName || "Cockpit proposal";
    var h = 7;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 100;
    var code = "Z" + ("0" + h).slice(-2);
    if (_feedRailcardCodes()[code]) code = "Q" + ("0" + h).slice(-2);
    return code;
  }

  function buildChangeRequest(componentInput, selCorridor, scope) {
    var params = componentInput.params || {};
    var corridor = _corridors[0];
    if (selCorridor) {
      var found = _corridors.filter(function (c) { return c.id === selCorridor; })[0];
      if (found) corridor = found;
    }
    // Discount must be strictly 0 < x < 1 per ChangeRequest validation.
    var pct = Math.max(1, Math.min(60, params.discountPct || 34)) / 100.0;
    // changeType 'raise' proposes an across-the-board price rise
    // (kind='raise_price'); every other changeType is a railcard discount.
    var isRise = params.changeType === "raise";
    var out = {
      kind: isRise ? "raise_price" : "add_railcard",
      railcard_code: _proposalCode(params),
      discount_pct: pct,
      // 01=Anytime, 03=Off-Peak R (SVR), 08=Super Off-Peak R (OPR): the
      // regulated walk-ups live in 03/08, so scoping only 01 made every
      // compliance panel read "0 regulated" (nothing regulated was touched).
      discount_categories: ["01", "03", "08"],
      corridor_origin_nlc: corridor ? corridor.origin_nlc : "2968",
      corridor_dest_nlc: corridor ? corridor.dest_nlc : "1444",
      peak_valid: !!params.peakOn,
      description: (params.railcardName || "Cockpit proposal") +
                   " \u00b7 " + (isRise ? "+" : "") + Math.round(pct * 100) + "%",
      rounding_rule: params.rounding || null,
      min_floor_pct: params.minFloorPct ? (params.minFloorPct / 100.0) : null,
      cluster_name: params.cluster || null,
      contradiction_choice: null,
    };
    // Operator scope: the ChangeRequest contract requires empty corridor
    // NLCs when scope='toc' (change_request.py validation).
    if (scope && scope.kind === "toc" && scope.toc) {
      out.scope = "toc";
      out.toc_code = scope.toc;
      out.corridor_origin_nlc = "";
      out.corridor_dest_nlc = "";
    }
    return out;
  }

  function _scopeFromRoot() {
    // Mirrors how selCorridor is read: the Component owns the scope state,
    // we only translate it into ChangeRequest fields.
    var root = window.__rfeRoot;
    var st = root && root.state ? root.state : null;
    if (st && st.scopeKind === "toc" && st.selToc) return { kind: "toc", toc: st.selToc };
    return null;
  }

  function _requestRerender() {
    var root = window.__rfeRoot;
    if (root && typeof root.recompute === "function") {
      try { root.recompute(true); } catch (e) { console.warn("[rfe] rerender failed", e); }
    }
  }

  /* Boot-phase surface for the delivered UI's veil. The Component polls
     window.RFE_BOOT while window.RFE is absent, so a multi-minute cold feed
     warm shows an honest status instead of a dead screen. */
  function _setBoot(phase, detail) {
    window.RFE_BOOT = { phase: phase, detail: detail || "" };
  }
  _setBoot("waiting-backend", "contacting backend\u2026");

  function _notifyUi(text, sev) {
    // Fetch failures must be visible in the cockpit ticker, not console-only.
    var root = window.__rfeRoot;
    if (root && typeof root.pushFeed === "function") {
      try {
        root.pushFeed({ kind: "sys", severity: sev || "warn", source: "API", text: text });
      } catch (e) { /* ticker is cosmetic; never break the data path */ }
    }
  }

  /* ---- impact ---------------------------------------------------------- */
  function _fetchImpact(componentInput, selCorridor, scope) {
    var change;
    try { change = buildChangeRequest(componentInput, selCorridor, scope); }
    catch (e) { console.error("[rfe] buildChangeRequest threw", e); return; }
    var includes = Object.keys(componentInput.includes || {})
      .filter(function (k) { return componentInput.includes[k]; })
      // The delivered UI's "validity" toggle is the backend's anomalies module.
      .map(function (k) { return k === "validity" ? "anomalies" : k; })
      .filter(function (k) {
        return ["compliance","anomalies","revenue","splits","performance","revenue_odm","demand","carbon"].indexOf(k) >= 0;
      });
    // Eligible-share knob (Statistics tab slider). Read from the Component's
    // state; backend default (15%) applies when absent. Part of the request
    // signature so slider moves refetch, and late responses are discarded.
    var root = window.__rfeRoot;
    var esPct = root && root.state && typeof root.state.eligibleSharePct === "number"
      ? root.state.eligibleSharePct : null;
    var eligibleShare = (esPct && esPct > 0 && esPct <= 100) ? esPct / 100.0 : null;
    var sig = sigOf({ change: change, includes: includes, es: eligibleShare });
    if (sig === _lastImpactSig && _lastImpact) return;
    _lastImpactSig = sig;
    A.impact(change, includes, eligibleShare).then(function (data) {
      if (sig !== _lastImpactSig) return;  // late-response guard
      _lastImpactRaw = data;
      _lastImpact = ADAPT.adaptImpact(data, { stationsByNlc: _stationsByNlc });
      _lastImpactDataSig = sig; // settled: cache now matches the wanted sig
      _requestRerender();
    }).catch(function (e) {
      if (e && e.name === "AbortError") return;
      console.error("[rfe] /api/impact failed", e && e.body ? e.body : e);
      _lastImpactSig = null; // allow the next recompute to retry this change
      _lastImpactDataSig = null;
      _notifyUi("/api/impact failed \u2014 " +
        (e && e.body && e.body.detail ? e.body.detail : (e && e.message) || "network error"));
      // Settle the veil WITHOUT recompute(): a recompute would refetch the
      // same failing request in a loop. Retry happens on the next user action.
      var root = window.__rfeRoot;
      if (root && typeof root.setState === "function") {
        try {
          root.setState({ computing: false, resultsCurrent: true,
            computeError: "/api/impact failed \u2014 showing last computed result" });
        } catch (e2) { /* cosmetic only */ }
      }
    });
  }

  function _activeFeedRailcard() {
    // The railcard whose §4.17 discount chain /api/resolve can actually
    // trace: the Component's selected card, IF it exists in the .RLC feed.
    // Synthetic/staged cards aren't in the feed — their "after" numbers come
    // from /api/impact rows, so resolve falls back to the undiscounted chain.
    var root = window.__rfeRoot;
    var id = root && root.state ? root.state.activeRailcard : null;
    if (!id) return null;
    for (var i = 0; i < _railcards.length; i++) {
      if (_railcards[i].code === id && _railcards[i].in_feed) return _railcards[i].code;
    }
    return null;
  }

  // Flow keys carry CRS when the station is on the map, else a bare NLC
  // (group NLCs like 0438 have no coords entry). Accept both.
  function _lookupStation(tok) {
    return _stationsByCrs[tok] || _stationsByNlc[tok] || null;
  }
  function _tokenNlc(tok) {
    var st = _lookupStation(tok);
    if (st && st.nlc) return st.nlc;
    // Bare NLC (e.g. group 0438) or .FSC cluster id (e.g. T113) —
    // /api/resolve accepts both 4-char code kinds directly. Rejecting
    // cluster ids here left their rows on the loading stub forever (the
    // old fabricated-£0.00 path).
    return /^[A-Za-z0-9]{4,}$/.test(String(tok)) ? String(tok) : null;
  }

  function _fetchResolveFor(cacheKey, oCrs, dCrs, code, railcard) {
    if (_resolveInflight.has(cacheKey)) return;
    var oNlc = _tokenNlc(oCrs), dNlc = _tokenNlc(dCrs);
    if (!oNlc || !dNlc) return;
    _resolveInflight.add(cacheKey);
    A.resolve(oNlc, dNlc, code, railcard ? { railcard: railcard } : {}).then(function (data) {
      _resolveInflight["delete"](cacheKey);
      _resolveCache.set(cacheKey, ADAPT.adaptResolve(data));
      _requestRerender();
    }).catch(function (e) {
      _resolveInflight["delete"](cacheKey);
      if (e && e.name === "AbortError") return;
      console.error("[rfe] /api/resolve failed for", cacheKey, e);
      _notifyUi("/api/resolve failed for " + cacheKey + " \u2014 " +
        (e && e.body && e.body.detail ? e.body.detail : (e && e.message) || "network error"));
    });
  }

  /* ---- corridor stats + network overview (sync cache, async fetch) ----- */
  function _fetchCorridorStats(key, oCrs, dCrs) {
    if (_cstatsInflight.has(key)) return;
    _cstatsInflight.add(key);
    A.corridorStats(oCrs, dCrs).then(function (data) {
      _cstatsInflight["delete"](key);
      _cstatsCache.set(key, data);
      _requestRerender();
    }).catch(function (e) {
      _cstatsInflight["delete"](key);
      if (e && e.name === "AbortError") return;
      console.error("[rfe] /api/corridor/stats failed for", key, e);
      _cstatsCache.set(key, { error: (e && e.body && e.body.detail) || (e && e.message) || "fetch failed" });
      _requestRerender();
    });
  }

  function _fetchOverview() {
    if (_overviewInflight) return;
    _overviewInflight = true;
    A.overview().then(function (data) {
      _overviewInflight = false;
      _overviewCache = data;
      _requestRerender();
      // Overview is computed off-thread at backend boot; poll until ready.
      if (data && data.ready === false) {
        setTimeout(function () { _fetchOverview(); }, 4000);
      }
    }).catch(function (e) {
      _overviewInflight = false;
      if (e && e.name === "AbortError") return;
      console.error("[rfe] /api/overview failed", e);
      _overviewCache = { ready: false, error: (e && e.body && e.body.detail) || (e && e.message) || "fetch failed", corridors: [], notes: [] };
      _requestRerender();
    });
  }

  /* ---- surface constructors ------------------------------------------- */
  function _makeStations() {
    return _stations.map(function (s) {
      return { id: s.crs, name: s.name, x: s.x, y: s.y, region: "gb" };
    });
  }
  function _makeCorridors() {
    return _corridors.map(function (c) {
      return {
        id: c.id, name: c.name, sub: c.sub,
        path: c.path_crs, endpoints: c.origin_crs + " \u2013 " + c.dest_crs,
        defFlow: c.origin_crs + "-" + c.dest_crs + ":" + (c.default_ticket || "SOR"),
        _originNlc: c.origin_nlc, _destNlc: c.dest_nlc,
        _toc: c.toc,
      };
    });
  }
  function _makeEdges() {
    // Rail-line skeleton: consecutive path_crs pairs across all corridors,
    // deduped, each edge remembering which corridor(s) it belongs to so the
    // map can brighten the selected spine.
    var byKey = {};
    _corridors.forEach(function (c) {
      var path = c.path_crs || [];
      for (var i = 0; i < path.length - 1; i++) {
        var a = path[i], b = path[i + 1];
        if (!_stationsByCrs[a] || !_stationsByCrs[b]) continue;
        var key = a < b ? a + "-" + b : b + "-" + a;
        var e = byKey[key];
        if (!e) { e = byKey[key] = { a: a, b: b, cids: [] }; }
        if (e.cids.indexOf(c.id) < 0) e.cids.push(c.id);
      }
    });
    return Object.keys(byKey).map(function (k) { return byKey[k]; });
  }

  function _makeFlowsSparse() {
    // The delivered code walks Object.values(RFE.FLOWS) in `seedFeed` and
    // in some ticker/synth helpers. We give it one flow per corridor so
    // those loops have something to iterate over; the shape mirrors the
    // mock's FLOWS entries.
    var out = {};
    _corridors.forEach(function (c) {
      var key = c.origin_crs + "-" + c.dest_crs;
      out[key] = {
        o: c.origin_crs, d: c.dest_crs,
        km: euclideanKm(_stationsByCrs[c.origin_crs], _stationsByCrs[c.dest_crs]),
        seq: c.id,
        products: [{ code: c.default_ticket || "SOR", name: "Off-Peak Return", baseP: 0, offpeak: true }],
        regulated: false, protectedPct: 0, peakRestricted: false,
        override: null,
        volume: 0, volumeExact: false,
        rcJourneys: 0, rcJourneysExact: false,
        callingPoints: c.path_crs.slice(1, -1),
        routeCode: "00000",
        contested: false, policy: null,
      };
    });
    return out;
  }

  function _makeTocStations() {
    // {fareToc: [CRS,...]} — the operator's network for map lighting. Joined
    // NLC→CRS via /api/stations; NLCs without map coords (groups, clusters)
    // are simply not lit.
    var out = {};
    _tocs.forEach(function (t) {
      var crses = [];
      (t.station_nlcs || []).forEach(function (nlc) {
        var st = _stationsByNlc[nlc];
        if (st && st.crs) crses.push(st.crs);
      });
      out[t.code] = crses;
    });
    return out;
  }

  function _resolveStub(_o, _d, code) {
    return {
      steps: [{ key: "loading", label: "loading provenance\u2026", active: false,
                inP: null, outP: null,
                note: "fetching from /api/resolve",
                cite: "\u2014", source: null }],
      finalP: null, status: "pending",
      prod: { name: ADAPT.ticketName(code) },
      inScheme: true, unresolved: false, railcardExcluded: false,
    };
  }

  function _overlayProposal(base, oCrs, dCrs, code) {
    // The active cockpit card is synthetic (not in .RLC), so /api/resolve
    // cannot trace its discount chain. The staged "after" price is read off
    // the impact engine's canonical_affected rows — NEVER computed here
    // (CLAUDE.md: prices only ever come from the resolver/impact engine).
    var raw = _lastImpactRaw;
    if (!raw || !raw.canonical_affected || !raw.canonical_affected.length) return base;
    var oNlc = _tokenNlc(oCrs), dNlc = _tokenNlc(dCrs);
    if (!oNlc || !dNlc) return base;
    var row = null;
    for (var i = 0; i < raw.canonical_affected.length; i++) {
      var af = raw.canonical_affected[i];
      if (af.ticket_code !== code || af.new_price_pence == null) continue;
      var repMatch =
        (af.representative_origin_nlc === oNlc && af.representative_dest_nlc === dNlc) ||
        (af.representative_origin_nlc === dNlc && af.representative_dest_nlc === oNlc);
      if (repMatch) { row = af; break; }
      var blast = af.blast_station_nlcs || [];
      if (!row && blast.indexOf(oNlc) >= 0 && blast.indexOf(dNlc) >= 0) row = af;
    }
    if (!row) return base; // flow untouched by the proposal — honest baseline
    var change = raw.change || {};
    var out = {};
    for (var k in base) {
      if (Object.prototype.hasOwnProperty.call(base, k)) out[k] = base[k];
    }
    out.finalP = row.new_price_pence;
    // The overlaid price is real (impact engine, canonical_affected row) —
    // a noFare flag inherited from the base chain no longer applies.
    out.noFare = false;
    out.statusHeadline = "";
    // The proposal step becomes the terminal one; demote the base chain's
    // terminal via a clone (the cached base object must stay untouched).
    var steps = (base.steps || []).slice();
    if (steps.length && steps[steps.length - 1].terminal) {
      var demoted = {};
      var prior = steps[steps.length - 1];
      for (var k2 in prior) {
        if (Object.prototype.hasOwnProperty.call(prior, k2)) demoted[k2] = prior[k2];
      }
      demoted.terminal = false;
      steps[steps.length - 1] = demoted;
    }
    steps.push({
      key: "proposal_preview",
      label: "proposed change (impact engine)",
      active: true,
      terminal: true,
      inP: row.old_price_pence != null ? row.old_price_pence : base.finalP,
      outP: row.new_price_pence,
      note: (change.description || "cockpit proposal") +
            " \u00b7 price from /api/impact, flow " + row.flow_id,
      cite: "proposal diff \u2014 baseline untouched",
      source: null,
    });
    out.steps = steps;
    return out;
  }

  function _emptyImpact() {
    return {
      rows: [], counts: { total: 0, clean: 0, anomaly: 0, breach: 0, contradiction: 0 },
      verdict: { affectedTotal: 0, cleanRows: 0, implicatedRows: 0,
                 inversions: null, breaches: null, contradictions: 0,
                 splitsOpened: null, revenue: null },
      anomalies: [], splits: [], breaches: [], contradictions: [], stationStats: {},
      revenue: { totalP: 0, exactP: 0, estP: 0, journeys: 0, exactShare: 0,
                 method: null, methodLabel: "", periodLabel: "",
                 perFlowExposureP: null, perPairExposureP: null,
                 odmRows: null, matchedFlows: 0, unmatchedFlows: 0,
                 fareRows: [], notes: [] },
      demand: null, carbon: null, complianceMeta: null,
      notes: [], have: {},
    };
  }

  /* ---- expose RFE ----------------------------------------------------- */
  function _publish(defaultCorridor) {
    // CLUSTER_LABEL carries BOTH the mock's legacy keys (so the delivered
    // pill group still shows human names) AND our real cluster ids from
    // cluster_labels.json — aliases keep the template from crashing.
    var CLUSTER_LABEL = {
      scotland: "ScotRail regional",
      highland: "Highland lines",
      tayfife: "Tay & Fife corridor",
      national: "National (all stations)",
      scotrail_regional: "ScotRail regional",
      wcml_north: "West Coast Main Line",
      ecml: "East Coast Main Line",
      gwml_south: "Great Western Main Line",
    };

    window.RFE = {
      SNAPSHOT: {
        feed: _snapshot ? _snapshot.feed : "RJFAF",
        id: _snapshot ? _snapshot.id : "\u2014",
        date: _snapshot ? _snapshot.date : "\u2014",
        records: _snapshot ? _snapshot.records : 0,
        // We don't have a distinct flow count from the backend today, so
        // the topbar's `flows` binding shows the record count too — honest
        // and the number is what any analyst would want to see anyway.
        flows: _snapshot ? _snapshot.records : 0,
      },
      STATIONS: _makeStations(),
      ST: _stationsByCrs,
      EDGES: _makeEdges(),
      CORRIDORS: _makeCorridors(),
      CLUSTERS: {},
      CLUSTER_LABEL: CLUSTER_LABEL,
      FLOWS: _makeFlowsSparse(),
      RAILCARDS: _railcards,
      TOCS: _tocs,
      TOC_STATIONS: _makeTocStations(),

      dist: function (a, b) {
        return euclideanKm(_stationsByCrs[a], _stationsByCrs[b]);
      },
      getFlow: function (o, d) {
        var k = o + "-" + d;
        if (window.RFE.FLOWS[k]) return window.RFE.FLOWS[k];
        return window.RFE.FLOWS[d + "-" + o] || null;
      },
      flowKey: function (o, d, code) { return o + "-" + d + ":" + code; },
      parseKey: function (k) {
        var parts = String(k).split(":");
        var od = parts[0].split("-");
        return { o: od[0], d: od[1], code: parts[1] };
      },
      product: function (flow, code) {
        for (var i = 0; i < flow.products.length; i++) {
          if (flow.products[i].code === code) return flow.products[i];
        }
        return flow.products[0];
      },
      flowInScheme: function (_f, _c) { return true; },
      inCluster: function (_crs, _cluster) { return true; },
      roundingLabel: function (r) {
        return ({ near5: "Round to nearest 5p", near10: "Round to nearest 10p",
                  down10: "Round down to 10p", none: "No rounding" })[r] || r;
      },
      fmtMoney: ADAPT.fmtMoney,
      fmtDelta: ADAPT.fmtDelta,
      fmtBig: ADAPT.fmtBig,
      fmtBigDir: ADAPT.fmtBigDir,
      fmtInt: ADAPT.fmtInt,
      fmtKg: ADAPT.fmtKg,
      fmtPct: ADAPT.fmtPct,

      /* sync surface, async underneath — see file header for contract.
         `withRailcard` selects the discounted chain (feed railcards only);
         the cache key carries the railcard so before/after are distinct. */
      resolve: function (o, d, code, _params, _disp, withRailcard) {
        var railcard = withRailcard ? _activeFeedRailcard() : null;
        var key = o + "-" + d + ":" + code + ":" + (railcard || "base");
        var base = _resolveCache.get(key);
        if (!base) {
          _fetchResolveFor(key, o, d, code, railcard);
          base = _resolveStub(o, d, code);
        }
        // Feed railcards get their discounted chain straight from
        // /api/resolve; synthetic proposal cards overlay the staged price
        // from the latest impact rows so the headline fare and the
        // provenance's before/after actually follow the analyst's sliders.
        if (!withRailcard || railcard) return base;
        return _overlayProposal(base, o, d, code);
      },
      computeImpact: function (input) {
        var selCorridor = (window.__rfeRoot && window.__rfeRoot.state)
          ? window.__rfeRoot.state.selCorridor : (defaultCorridor || null);
        _fetchImpact(input, selCorridor, _scopeFromRoot());
        return _lastImpact || _emptyImpact();
      },
      // True while an impact fetch is in flight for the current signature —
      // i.e. the cache computeImpact just returned is NOT the wanted result.
      impactPending: function () {
        return _lastImpactSig !== null && _lastImpactSig !== _lastImpactDataSig;
      },
      // Corridor fact sheet (Statistics tab). Returns cached payload or null
      // while the first fetch is in flight; rerender fires when it lands.
      corridorStats: function (oCrs, dCrs) {
        var key = oCrs + "-" + dCrs;
        var hit = _cstatsCache.get(key);
        if (!hit) _fetchCorridorStats(key, oCrs, dCrs);
        return hit || null;
      },
      // Network overview (Master tab). Cached; force=true refetches (staging
      // counts overlay changes when a card is proposed/approved).
      overview: function (force) {
        if (force || !_overviewCache) _fetchOverview();
        return _overviewCache;
      },
      invalidateOverview: function () { _overviewCache = null; },

      /* staging: propose returns {kind:'accepted',card,layer} | {kind:'escalation',...} */
      proposeChange: function (input, selCorridor, contradictionChoice) {
        var change = buildChangeRequest(input, selCorridor, _scopeFromRoot());
        if (contradictionChoice) change.contradiction_choice = contradictionChoice;
        return A.propose(change);
      },
      /* custom corridors — free-form OD pairs from the journey lookup.
         The path is timetable-derived server-side (/api/route); we only
         register it so the map spine, edge skeleton and impact scoping
         (origin/dest NLC) treat it exactly like a curated corridor. */
      fetchRoute: function (o, d) { return A.route(o, d); },
      addCustomCorridor: function (r) {
        var id = "custom-" + r.origin_crs + "-" + r.dest_crs;
        for (var i = 0; i < _corridors.length; i++) {
          if (_corridors[i].id === id) return id;
        }
        _corridors.push({
          id: id, name: r.name, sub: r.sub,
          origin_crs: r.origin_crs, origin_nlc: r.origin_nlc,
          dest_crs: r.dest_crs, dest_nlc: r.dest_nlc,
          default_ticket: "SOR", toc: "", path_crs: r.path_crs,
        });
        window.RFE.CORRIDORS = _makeCorridors();
        window.RFE.EDGES = _makeEdges();
        window.RFE.FLOWS = _makeFlowsSparse();
        return id;
      },
      approveCard: function (cardId) { return A.approve(cardId); },
      fetchStaging: function () { return A.staging(); },
      resetStaging: function () { return A.resetStaging(); },
      proposalCodeFor: function (params) { return _proposalCode(params || {}); },

      _api: A,
      _raw: function () { return _lastImpactRaw; },
      envMode: A.getEnvMode(),
      setEnvMode: function (m) {
        A.setEnvMode(m);
        window.RFE.envMode = A.getEnvMode();
        // Mode changes the HSP source (live vs fixture) — invalidate the
        // impact signature so the next computeImpact refetches.
        _lastImpactSig = null;
      },
    };
  }

  /* ---- boot ----------------------------------------------------------- */
  function _healthGate(done) {
    // Block the metadata fetches until the backend reports warm — a cold
    // boot parses the whole .FFL and every request hangs meanwhile. The
    // veil reads window.RFE_BOOT for the honest status.
    A.health(4000).then(function (h) {
      if (h && h.warm) {
        _setBoot("loading", "backend warm \u2014 fetching metadata\u2026");
        done();
        return;
      }
      _setBoot("warming", "backend warming feed indexes (snapshot " +
        ((h && h.snapshot_id) || "?") + ") \u2014 the first boot per snapshot parses the full .FFL");
      setTimeout(function () { _healthGate(done); }, 2000);
    }, function (e) {
      if (e && e.status === 404) { done(); return; } // backend predates /api/health
      _setBoot("waiting-backend",
        "backend unreachable \u2014 serve the UI from the API origin (uvicorn src.api.main:app), retrying\u2026");
      setTimeout(function () { _healthGate(done); }, 2000);
    });
  }

  function boot() {
    _healthGate(function () {
      Promise.all([
        A.snapshot(), A.corridors(), A.stations(), A.railcards(),
        // Older backends lack /api/tocs — operator scoping degrades to
        // an empty picker rather than blocking boot.
        A.tocs()["catch"](function () { return []; }),
      ])
        .then(function (results) {
          _snapshot = results[0];
          _corridors = results[1];
          _stations = results[2];
          _railcards = results[3];
          _tocs = results[4] || [];
          _stations.forEach(function (s) {
            _stationsByCrs[s.crs] = s;
            if (s.nlc) _stationsByNlc[s.nlc] = s;
          });
          _publish(_corridors.length ? _corridors[0].id : null);
          _setBoot("ready", "");
          // Kick a first impact fetch so the aggregate panel is populated by
          // the time the Component's boot-time `recompute(true)` lands its
          // 300ms setTimeout — one less "empty" frame at boot.
          _fetchImpact({
            params: { discountPct: 34, peakOn: false, rounding: "near10",
                      cluster: "national", minFloorPct: 55,
                      railcardName: "Cockpit boot" },
            // demand is on by boot so the landing's basis-chip strip can cite
            // the elasticity source, eligible-share assumption and modelled
            // volume from the first render — no "off" chip on the front door.
            includes: { compliance: true, revenue: true, anomalies: true,
                        splits: true, performance: true, demand: true },
            disp: {},
          }, _corridors.length ? _corridors[0].id : null);
        })
        .catch(function (e) {
          // Do NOT publish an empty RFE here: the Component adopts engine
          // defaults exactly once, so a degraded publish would freeze empty
          // corridor/railcard rails even after the backend recovers.
          console.error("[rfe] boot failed; retrying in 3s", e);
          _setBoot("error", "metadata fetch failed \u2014 retrying\u2026");
          setTimeout(boot, 3000);
        });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
