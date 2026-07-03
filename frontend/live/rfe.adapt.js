/* rfe.adapt.js
   Pure adapter layer: backend response shape → template-ready shape.
   No fetch, no globals except read-only reference tables passed in.
   One function per section. Every input `null` block is treated as "module
   off"; empty arrays as "clean" — the two are never conflated (per
   correctness rule 5 in the plan).
*/
(function () {
  "use strict";

  function fmtMoney(p) {
    if (p == null) return "\u2014";
    var neg = p < 0; p = Math.abs(p);
    return (neg ? "\u2212\u00a3" : "\u00a3") + (p / 100).toFixed(2);
  }
  function fmtDelta(p) {
    if (p == null) return "\u2014";
    var sign = p > 0 ? "+\u00a3" : p < 0 ? "\u2212\u00a3" : "\u00a3";
    return sign + (Math.abs(p) / 100).toFixed(2);
  }
  function fmtBig(p) {
    var neg = p < 0; p = Math.abs(Math.round(p / 100));
    var s = p.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (neg ? "\u2212\u00a3" : "\u00a3") + s;
  }
  // Revenue headline totals: a bare minus is easy to miss at a glance, so
  // spell the direction out — "£88,150 loss" / "£12,300 gain".
  function fmtBigDir(p) {
    var s = "\u00a3" + Math.abs(Math.round(p / 100)).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return p < 0 ? s + " loss" : (p > 0 ? s + " gain" : s);
  }
  function fmtInt(n) {
    if (n == null) return "\u2014";
    var neg = n < 0; n = Math.abs(Math.round(n));
    var s = n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (neg ? "\u2212" : "") + s;
  }
  function fmtKg(kg) {
    if (kg == null) return "\u2014";
    if (Math.abs(kg) >= 1000) return (kg / 1000).toFixed(2) + " tCO\u2082e";
    return kg.toFixed(1) + " kgCO\u2082e";
  }
  function fmtPct(x, dp) {
    if (x == null) return "\u2014";
    return x.toFixed(dp == null ? 1 : dp) + "%";
  }

  /* Backend AffectedFare → the row shape the delivered template's blast
     table + map fan-out reads. Missing volume/rcJourneys → sensible
     placeholder (marked NOT-exact via `volumeExact=false` so the UI
     stripes / labels can show the honest "modelled" tag). */
  function ticketName(code) {
    return code === "SOR" ? "Off-Peak Return"
         : code === "SDS" ? "Anytime Day Single"
         : (code || "\u2014");
  }

  // Prefer the map station name, then the backend .LOC name (covers group
  // NLCs like 0438 = "MANCHESTER GRP"). Letter-prefixed codes with no name
  // are .FSC cluster ids (station NLCs are 4-digit numeric) — label them
  // honestly instead of echoing the raw code.
  function entityLabel(code, locName, stationsByNlc) {
    var st = stationsByNlc[code] || {};
    if (st.name) return st.name;
    // Backend echoes the raw code as the name when there is no LOC entry.
    if (locName && locName !== code) return locName;
    if (/^[A-Za-z]/.test(code || "")) return "Cluster " + code;
    return code;
  }

  function adaptAffectedFare(af, opts) {
    opts = opts || {};
    var stationsByNlc = opts.stationsByNlc || {};
    var oCrs = (stationsByNlc[af.representative_origin_nlc] || {}).crs || af.representative_origin_nlc;
    var dCrs = (stationsByNlc[af.representative_dest_nlc] || {}).crs || af.representative_dest_nlc;
    var oName = entityLabel(af.representative_origin_nlc, af.representative_origin_name, stationsByNlc);
    var dName = entityLabel(af.representative_dest_nlc, af.representative_dest_name, stationsByNlc);
    var deltaP = (af.new_price_pence != null && af.old_price_pence != null)
      ? (af.new_price_pence - af.old_price_pence) : null;
    var comp = af.compliance || null;
    var verdict = "not-regulated";
    var bucket = "clean";
    // Never null — buildBlast reads r.compliance.label unconditionally. When
    // the compliance include is off, the honest label is a dash, not a
    // fabricated verdict (absent module ≠ "unregulated").
    var compliance = { label: comp ? "UNREG" : "\u2014", floorP: null, cite: "",
                       citeSection: "", citeRule: "", evidence: "", explanation: "", status: comp ? comp.status : null };
    if (comp) {
      if (comp.status === "breach") { verdict = "breach"; bucket = "breach"; compliance.label = "BREACH"; }
      else if (comp.status === "compliant") { verdict = "compliant"; compliance.label = "CAP OK"; }
      compliance.floorP = comp.cap_price_2025_pence;
      compliance.explanation = comp.explanation || "";
      if (comp.citation) {
        compliance.citeSection = comp.citation.section || "";
        compliance.citeRule = comp.citation.rule_text || "";
        compliance.evidence = comp.citation.evidence || "";
        compliance.cite = compliance.citeSection + " \u00b7 " + compliance.citeRule;
      }
    }
    if (af.status === "contradiction") { verdict = "pending"; bucket = "contradiction"; compliance.label = "HELD"; }
    var oSt = stationsByNlc[af.representative_origin_nlc];
    var dSt = stationsByNlc[af.representative_dest_nlc];
    var km = 0;
    if (oSt && dSt && oSt.x != null && dSt.x != null) {
      // Station coords are SVG-projected OSGB E/N; 1 SVG unit ≈ 1.55 km
      // (same constant as fare-engine's euclideanKm).
      var dx = oSt.x - dSt.x, dy = oSt.y - dSt.y;
      km = Math.round(Math.sqrt(dx * dx + dy * dy) * 1.55);
    }
    // Cite the exact feed record behind the BEFORE price, so odd-looking
    // genuine values (e.g. the real 99900p VCJ fare) are traceable on hover.
    var srcNote = "";
    (af.provenance || []).some(function (p) {
      var d = p.detail || {};
      if (d.fare_line_no) {
        srcNote = "Before price from feed: " + p.source +
          " \u00b7 T-record line " + d.fare_line_no;
        return true;
      }
      return false;
    });
    return {
      // Key format must match RFE.parseKey ("O-D:code") — the delivered UI
      // feeds this straight into selectFlow → parseKey → resolve.
      key: oCrs + "-" + dCrs + ":" + af.ticket_code,
      o: oCrs, d: dCrs, code: af.ticket_code,
      flowId: af.flow_id,
      oName: oName, dName: dName,
      productName: ticketName(af.ticket_code),
      km: km,
      beforeP: af.old_price_pence,
      afterP: af.new_price_pence,
      deltaP: deltaP,
      railP: af.new_price_pence,
      verdict: verdict,
      compliance: compliance,
      unresolved: af.status === "contradiction",
      volume: 0, volumeExact: false,
      rcJourneys: 0, rcJournExact: false,
      revenueDeltaP: 0, revenueExact: false,
      flags: [], bucket: bucket,
      srcNote: srcNote,
      // Provenance is per-fare in the backend; keep an id for lazy resolve.
      _originNlc: af.representative_origin_nlc,
      _destNlc: af.representative_dest_nlc,
      _blastNlcs: af.blast_station_nlcs || [],
    };
  }

  function adaptImpact(report, opts) {
    if (!report) return null;
    opts = opts || {};
    var stationsByNlc = opts.stationsByNlc || {};
    var rows = (report.canonical_affected || [])
      .map(function (af) { return adaptAffectedFare(af, { stationsByNlc: stationsByNlc }); });
    var counts = { total: rows.length, clean: 0, anomaly: 0, breach: 0, contradiction: 0 };
    rows.forEach(function (r) { counts[r.bucket] = (counts[r.bucket] || 0) + 1; });

    // Compliance block — never conflate absent (null block) with clean (0 breaches).
    var breaches = [];
    var complianceMeta = null;
    if (report.compliance) {
      breaches = (report.compliance.breaches || []).map(function (b) {
        return adaptAffectedFare(b, { stationsByNlc: stationsByNlc });
      });
      // Backend-authoritative counts (joined over the full/retained set) —
      // never re-count client-side from the possibly-truncated row list.
      complianceMeta = {
        regulatedCount: report.compliance.regulated_count,
        breachCount: report.compliance.breach_count,
        partial: !!report.compliance.partial,
        notes: report.compliance.regulation_map_notes || [],
      };
    }

    // Anomaly block — same rule.
    var anomalies = [];
    if (report.anomalies) {
      anomalies = (report.anomalies.inversions || []).map(function (inv) {
        var oCrs = (stationsByNlc[inv.origin_nlc] || {}).crs || inv.origin_nlc;
        var dCrs = (stationsByNlc[inv.dest_nlc] || {}).crs || inv.dest_nlc;
        // Display labels prefer CRS; unmapped cluster ids read "Cluster T122"
        // rather than a bare code. Keys keep the raw token (must match rows).
        var oLab = (stationsByNlc[inv.origin_nlc] || {}).crs || entityLabel(inv.origin_nlc, null, stationsByNlc);
        var dLab = (stationsByNlc[inv.dest_nlc] || {}).crs || entityLabel(inv.dest_nlc, null, stationsByNlc);
        return {
          key: oCrs + "-" + dCrs + ":" + inv.higher_ticket + "|" + inv.lower_ticket,
          farKey: oCrs + "-" + dCrs + ":" + inv.higher_ticket,
          nearKey: oCrs + "-" + dCrs + ":" + inv.lower_ticket,
          kind: inv.rule,
          text: oLab + " \u2192 " + dLab + " " + inv.higher_ticket + " (" +
                fmtMoney(inv.higher_price_pence) + ") sits ABOVE " +
                inv.lower_ticket + " (" + fmtMoney(inv.lower_price_pence) + ")",
          detail: inv.explanation || "",
          cause: inv.rule,
        };
      });
    }

    // Rows implicated in an inversion get bucket "anomaly" so map/table
    // colouring agrees with the DETECTED list (they never did before: the
    // affected-fare adapter only knows compliance/contradiction). Precedence:
    // contradiction > breach > anomaly > clean. Counts are rebuilt so every
    // consumer sees the same numbers.
    var invKeys = {};
    anomalies.forEach(function (a) { invKeys[a.farKey] = true; invKeys[a.nearKey] = true; });
    rows.forEach(function (r) {
      if (r.bucket === "clean" && invKeys[r.key]) r.bucket = "anomaly";
    });
    counts.clean = 0; counts.anomaly = 0; counts.breach = 0; counts.contradiction = 0;
    rows.forEach(function (r) { counts[r.bucket] = (counts[r.bucket] || 0) + 1; });

    // Splits block.
    var splits = [];
    if (report.splits) {
      var pack = (report.splits.created || []).concat(report.splits.pre_change || []);
      pack.forEach(function (c) {
        if (c.saving_pence <= 0) return;
        var midCrs = (stationsByNlc[c.intermediate_nlc] || {}).crs;
        var midName = entityLabel(c.intermediate_nlc, midCrs, stationsByNlc);
        splits.push({
          key: report.splits.corridor_origin_nlc + "-" + report.splits.corridor_dest_nlc + ":" + c.ticket_code,
          o: report.splits.corridor_origin_nlc,
          d: report.splits.corridor_dest_nlc,
          oName: (stationsByNlc[report.splits.corridor_origin_nlc] || {}).name || "",
          dName: (stationsByNlc[report.splits.corridor_dest_nlc] || {}).name || "",
          throughP: c.through_price_pence,
          mid: c.intermediate_nlc, midName: midName,
          aP: c.leg1_price_pence, bP: c.leg2_price_pence,
          sumP: c.split_total_pence, saveP: c.saving_pence,
          verified: false,
          verifyNote: c.status === "opportunity" ? "Backend-computed candidate" : "Not timetable-verified",
        });
      });
      splits.sort(function (a, b) { return b.saveP - a.saveP; });
    }

    // Revenue — prefer the ODM block (richer) when present. Every number
    // here is read straight off backend fields (or summed from resolver-
    // computed per-fare deltas); nothing is priced client-side.
    var revenue = { totalP: 0, exactP: 0, estP: 0, journeys: 0, exactShare: 0,
                    method: null, methodLabel: "", periodLabel: "",
                    perFlowExposureP: null, perPairExposureP: null,
                    odmRows: null, matchedFlows: 0, unmatchedFlows: 0,
                    fareRows: [], notes: [] };
    if (report.revenue_odm && (report.revenue_odm.estimates || []).length) {
      var odm = report.revenue_odm;
      var total = odm.total_revenue_delta_pence;
      var exactPct = odm.matched_flow_count && (odm.matched_flow_count + odm.unmatched_flow_count)
        ? Math.round(100 * odm.matched_flow_count / (odm.matched_flow_count + odm.unmatched_flow_count))
        : 0;
      revenue.totalP = total;
      revenue.exactP = Math.round(total * exactPct / 100);
      revenue.estP = total - Math.round(total * exactPct / 100);
      revenue.journeys = odm.estimates.reduce(function (a, e) { return a + (e.journeys_per_period || 0); }, 0);
      revenue.exactShare = exactPct;
      revenue.method = "odm";
      revenue.methodLabel = "ODM journey-weighted revenue \u0394 (" + (odm.period_label || "per period") + ")";
      revenue.periodLabel = odm.period_label || "";
      revenue.matchedFlows = odm.matched_flow_count;
      revenue.unmatchedFlows = odm.unmatched_flow_count;
      revenue.odmRows = odm.estimates.map(function (e) {
        return {
          key: e.flow_id + ":" + e.ticket_code,
          flowId: e.flow_id, ticket: e.ticket_code,
          journeys: e.journeys_per_period,
          deltaPerJourneyP: e.delta_pence_per_journey,
          revenueDeltaP: e.revenue_delta_pence,
          matched: (e.matched_pair_count || 0) > 0,
        };
      });
      revenue.notes = odm.notes || [];
    } else if (report.revenue) {
      revenue.totalP = report.revenue.per_pair_exposure_pence || 0;
      revenue.estP = revenue.totalP;
      // Exposure ≠ zero journeys: demand-weighting is simply off. null keeps
      // the UI from rendering a fabricated "0 journeys/yr" next to a £ figure.
      revenue.journeys = null;
      revenue.method = "exposure";
      revenue.methodLabel = "static exposure \u00b7 demand-weighting off";
      revenue.perFlowExposureP = report.revenue.per_flow_exposure_pence;
      revenue.perPairExposureP = report.revenue.per_pair_exposure_pence;
      if (report.revenue_odm) revenue.notes = report.revenue_odm.notes || [];
      // When the ODM block was skipped entirely (null), its skip-note lands
      // in the top-level report notes — surface it on the revenue tab.
      if (!revenue.notes.length) {
        revenue.notes = (report.notes || []).filter(function (n) {
          return /revenue_odm|ODM/i.test(n);
        });
      }
    }
    if (report.revenue || report.revenue_odm) {
      // Per-fare detail: resolver-computed old→new deltas from the affected
      // set, sorted by |Δ|. Each row keeps the flow key so the UI can open
      // its provenance chain via the existing selectFlow → resolve panel.
      revenue.fareRows = rows
        .filter(function (r) { return r.deltaP != null; })
        .slice()
        .sort(function (a, b) { return Math.abs(b.deltaP) - Math.abs(a.deltaP); })
        .map(function (r) {
          return {
            key: r.key, o: r.o, d: r.d, code: r.code, flowId: r.flowId,
            oName: r.oName, dName: r.dName,
            productName: r.productName,
            beforeP: r.beforeP, afterP: r.afterP, deltaP: r.deltaP,
            complianceLabel: r.compliance ? r.compliance.label : "\u2014",
            bucket: r.bucket,
          };
        });
    }

    // Contradictions arrive on the propose path, not impact — impact-time
    // contradictions surface as affected fares with status === "contradiction".
    var contradictions = rows.filter(function (r) { return r.unresolved; });

    // Per-station stats for map colour, keyed by CRS. Fan out through each
    // fare's blast_station_nlcs (individual station NLCs, group-expanded by
    // the backend) so the map lights the true blast radius, not just the
    // two representative endpoints.
    var stationStats = {};
    rows.forEach(function (r) {
      var seen = {};
      (r._blastNlcs || []).forEach(function (nlc) {
        var st = stationsByNlc[nlc];
        if (!st || !st.crs || seen[st.crs]) return;
        seen[st.crs] = true;
        var s = stationStats[st.crs] = stationStats[st.crs] || { affected: 0, breach: 0, anomaly: 0, contradiction: 0, deltaP: 0 };
        s.affected++;
        if (r.bucket !== "clean") s[r.bucket] = (s[r.bucket] || 0) + 1;
        if (r.deltaP) s.deltaP += r.deltaP;
      });
      // Representative endpoints always count, even if outside the blast list.
      [r.o, r.d].forEach(function (crs) {
        if (seen[crs]) return;
        seen[crs] = true;
        var s = stationStats[crs] = stationStats[crs] || { affected: 0, breach: 0, anomaly: 0, contradiction: 0, deltaP: 0 };
        s.affected++;
        if (r.bucket !== "clean") s[r.bucket] = (s[r.bucket] || 0) + 1;
        if (r.deltaP) s.deltaP += r.deltaP;
      });
    });

    // Performance block — real HSP serviceMetrics for the corridor. Each
    // service row: headcode-ish label (TOC + planned departure), route, and
    // the tightest punctuality tolerance as the status.
    var performance = null;
    if (report.performance && report.performance.result) {
      var pr = report.performance.result;
      performance = {
        window: pr.from_date + " \u2192 " + pr.to_date + " \u00b7 " + pr.days,
        services: (pr.services || []).map(function (st) {
          var s = st.service || {};
          var pt = st.percent_tolerance || [];
          var best = pt.length ? pt[0] : null;
          return {
            hc: (s.toc_code || "?") + " " + (s.gbtt_ptd || ""),
            route: (s.origin_crs || "?") + " \u2192 " + (s.dest_crs || "?"),
            status: best
              ? ("\u00b1" + best[0] + "min " + Math.round(best[1]) + "%")
              : ((s.matched_services || 0) + " svc"),
            ok: best ? best[1] >= 80 : true,
          };
        }),
      };
    }

    // Operator (TOC) scope: aggregates upstream ran over the FULL canonical
    // set; the rows here are the backend's top-N by |Δ| cut. Surface the
    // true totals so headers can say "top N of TOTAL" honestly.
    var aggregate = null;
    var ss = report.scope_stats;
    if (ss && ss.scope === "toc") {
      aggregate = {
        isAggregate: true,
        toc: ss.toc_code,
        totalFares: ss.canonical_total,
        shownN: rows.length,
        flowsActual: ss.flows_actual,
        truncated: !!ss.truncated,
      };
      counts.total = ss.canonical_total;
    }

    // Demand block — elasticity-modelled ESTIMATES (never resolver prices).
    var demand = null;
    if (report.demand) {
      var db = report.demand;
      demand = {
        totalNetNewJourneys: db.total_net_new_journeys,
        flowsWithVolume: db.flows_with_volume,
        flowsPercentOnly: db.flows_percent_only,
        validityWarnings: db.validity_warnings,
        eligibleShare: db.eligible_share_assumption,
        notes: db.notes || [],
        rowRows: (db.estimates || []).map(function (e) {
          return {
            key: e.flow_id + ":" + e.ticket_code,
            flowId: e.flow_id, ticket: e.ticket_code,
            segment: e.ticket_segment, direction: e.direction,
            elasticity: e.elasticity,
            elasticityText: e.elasticity.toFixed(2) + (e.elasticity_derived ? " (derived)" : ""),
            elasticitySource: e.elasticity_source,
            priceChange: fmtPct(e.price_change_pct),
            grossDemandChange: fmtPct(e.gross_demand_change_pct),
            netNewJourneys: e.net_new_journeys,
            netNewText: e.net_new_journeys != null ? fmtInt(e.net_new_journeys) : "% only",
            withinValidity: e.within_validity,
            notes: e.notes || [],
          };
        }),
      };
    }

    // Carbon block — DEFRA-factor ESTIMATES over demand's net-new journeys.
    var carbon = null;
    if (report.carbon) {
      var cbk = report.carbon;
      carbon = {
        totalSavingKg: cbk.total_carbon_saving_kg,
        totalSavingText: fmtKg(cbk.total_carbon_saving_kg),
        corridorKm: cbk.corridor_distance_km,
        distanceMethod: cbk.corridor_distance_method,
        perPassengerKg: cbk.corridor_saving_kg_per_passenger,
        railKgPerPkm: cbk.rail_factor_kgco2e_per_pkm,
        railFactorDesc: cbk.rail_factor_description,
        carKgPerPkm: cbk.car_factor_kgco2e_per_pkm,
        tractionElectricPct: cbk.traction_electric_pct,
        tractionDieselPct: cbk.traction_diesel_pct,
        notes: cbk.notes || [],
        rowRows: (cbk.estimates || []).map(function (e) {
          return {
            key: e.flow_id + ":" + e.ticket_code,
            flowId: e.flow_id, ticket: e.ticket_code,
            netNewJourneys: e.net_new_journeys,
            distanceKm: e.distance_km,
            distanceMethod: e.distance_method,
            savingKg: e.carbon_saving_kg,
            savingText: fmtKg(e.carbon_saving_kg),
            notes: e.notes || [],
          };
        }),
      };
    }

    // ONE derived verdict — every displayed count (aggregate bucket lines,
    // review chips, DETECTED minis, tab badges, report modal) reads from
    // here, so no two same-labelled numbers can ever disagree. Inversions
    // are the DETECTION count (one inversion implicates two fare rows);
    // implicatedRows exists for visual weight (bars, map) only.
    // null = module off, never 0.
    var verdict = {
      affectedTotal: counts.total,
      cleanRows: counts.clean,
      implicatedRows: counts.anomaly,
      inversions: report.anomalies ? anomalies.length : null,
      breaches: complianceMeta ? complianceMeta.breachCount : null,
      contradictions: contradictions.length,
      splitsOpened: report.splits
        ? (report.splits.created || []).filter(function (c) { return c.saving_pence > 0; }).length
        : null,
      revenue: (report.revenue || report.revenue_odm) ? {
        totalP: revenue.totalP,
        method: revenue.method,
        journeys: revenue.journeys,
        est: revenue.method === "exposure" || revenue.exactShare < 100,
      } : null,
    };

    return {
      rows: rows, counts: counts,
      verdict: verdict,
      aggregate: aggregate,
      anomalies: anomalies, splits: splits, revenue: revenue,
      performance: performance,
      demand: demand, carbon: carbon,
      breaches: breaches, complianceMeta: complianceMeta,
      contradictions: contradictions,
      stationStats: stationStats,
      notes: report.notes || [],
      // Expose which analysis modules were populated so the UI can show
      // "module off" veils honestly (null block ≠ empty result).
      have: {
        compliance: !!report.compliance,
        anomalies: !!report.anomalies,
        revenue: !!(report.revenue || report.revenue_odm),
        splits: !!report.splits,
        performance: !!report.performance,
        demand: !!report.demand,
        carbon: !!report.carbon,
      },
    };
  }

  /* Staging escalation → Contradiction-tab shape. option_a is the already-
     staged card, option_b the incoming proposal; the analyst's pick becomes
     ChangeRequest.contradiction_choice["<flow_id>:<ticket_code>"] = "A"|"B". */
  function adaptEscalation(esc) {
    if (!esc || esc.kind !== "escalation") return null;
    // option_* values are all strings on the wire (dict[str,str]); prices
    // need parsing and may be "None" when a side suppresses the fare.
    function pence(v) { var n = parseInt(v, 10); return isNaN(n) ? null : n; }
    var pairs = (esc.contradictions || []).map(function (c) {
      var a = c.option_a || {}, b = c.option_b || {};
      return {
        key: c.flow_id + ":" + c.ticket_code,
        flowId: c.flow_id, ticket: c.ticket_code,
        optA: {
          title: "Keep existing card " + (a.source || "\u2014"),
          note: (a.change_description || "") +
                (a.provenance_summary ? " \u00b7 " + a.provenance_summary : ""),
          result: fmtMoney(pence(a.new_price_pence)),
        },
        optB: {
          title: "This proposal wins",
          note: (b.change_description || "") +
                (b.provenance_summary ? " \u00b7 " + b.provenance_summary : ""),
          result: fmtMoney(pence(b.new_price_pence)),
        },
      };
    });
    return {
      reason: esc.reason || "staged contradiction",
      cardIds: esc.existing_card_ids || [],
      pairs: pairs,
    };
  }

  /* Backend ResolvedFare → the delivered template's provenance chain shape.
     Each step becomes a card in the right panel; the raw fixed-width line
     the step cites populates the "SOURCE RECORD" inspector below. */
  function adaptResolve(rf) {
    if (!rf) return null;
    var steps = (rf.provenance || []).map(function (p, i) {
      var detail = p.detail || {};
      var noteFields = ["explanation", "note", "description", "NOTE", "EXPLANATION"];
      var note = "";
      for (var k = 0; k < noteFields.length; k++) {
        if (detail[noteFields[k]]) { note = detail[noteFields[k]]; break; }
      }
      if (!note) note = Object.keys(detail).slice(0, 3).map(function (k2) {
        return k2 + "=" + detail[k2];
      }).join(" \u00b7 ");
      // Step money: read (never compute) the first *_pence field the
      // resolver recorded, e.g. fare_record's FARE_pence.
      var outP = null;
      var dk = Object.keys(detail);
      for (var m = 0; m < dk.length; m++) {
        if (!/_pence$/i.test(dk[m])) continue;
        var v2 = detail[dk[m]];
        if (typeof v2 === "string" && /^\d+$/.test(v2)) v2 = parseInt(v2, 10);
        if (typeof v2 === "number" && isFinite(v2)) { outP = v2; break; }
      }
      return {
        key: p.step + ":" + i,
        label: p.step.replace(/_/g, " "),
        active: true,
        inP: null, outP: outP,
        note: note,
        cite: p.source,
        source: p.raw_record ? {
          title: p.step + " raw record",
          lines: [p.raw_record],
        } : null,
      };
    });
    // The last step is where the resolved price lands — mark it terminal so
    // the chain's final dot goes green with the settled amount.
    if (steps.length) {
      var last = steps[steps.length - 1];
      last.terminal = true;
      if (rf.price_pence != null) last.outP = rf.price_pence;
    }
    // Anything but "resolved" must never render as a price (£0.00 was the
    // old failure). noFare + an honest per-status headline; the provenance
    // chain stays — it is the explanation of the miss.
    var noFare = rf.status !== "resolved";
    var headline = "";
    if (noFare) {
      var tkt = ticketName(rf.ticket_code);
      headline =
          rf.status === "no_fare" ? "No fare found for " + tkt + " on this flow"
        : rf.status === "no_flow" ? "No flow record for this station pair"
        : rf.status === "suppressed" ? "Fare suppressed by NFO override (99999999)"
        : rf.status === "ambiguous" ? "Ambiguous feed records \u2014 quarantined, not guessed"
        : rf.status === "contradiction" ? "Contradiction held for human review"
        : "Unresolved (" + rf.status + ")";
    }
    return {
      steps: steps,
      finalP: rf.price_pence,
      status: rf.status,
      noFare: noFare,
      statusHeadline: headline,
      prod: { name: ticketName(rf.ticket_code) },
      inScheme: true,
      unresolved: rf.status === "contradiction",
      railcardExcluded: false,
    };
  }

  /* Merge point (visual-copilot session).
     /api/corridor/callings → {spine, stations}. `spine` keeps only stations
     a meaningful share of corridor trains call at (≥10% of through trains,
     min 2) so one slow Birmingham-loop diversion doesn't zigzag the drawn
     line; `stations` keeps the full union for the corridor strip. */
  function adaptCallings(raw) {
    if (!raw || !raw.found || !raw.callings || raw.callings.length < 2) return false;
    var thr = Math.max(2, Math.round((raw.direct_trains || 0) * 0.1));
    var spine = [];
    raw.callings.forEach(function (cp) {
      if (cp.trains_calling >= thr) spine.push(cp.crs);
    });
    return {
      spine: spine,
      stations: raw.callings,
      directTrains: raw.direct_trains,
      source: raw.source,
    };
  }

  window.RFE_ADAPT = {
    ticketName: ticketName,
    fmtMoney: fmtMoney,
    fmtDelta: fmtDelta,
    fmtBig: fmtBig,
    fmtBigDir: fmtBigDir,
    fmtInt: fmtInt,
    fmtKg: fmtKg,
    fmtPct: fmtPct,
    adaptImpact: adaptImpact,
    adaptResolve: adaptResolve,
    adaptCallings: adaptCallings,
    adaptAffectedFare: adaptAffectedFare,
    adaptEscalation: adaptEscalation,
  };
})();
