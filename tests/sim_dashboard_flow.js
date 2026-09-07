// End-to-end simulation of the dashboard flow for the new Satellite Risk filter.
// Mirrors frontend loadSchools() param building + renderTable() client sort EXACTLY.
const API = "http://127.0.0.1:8000";

const satRank = v => ({ Low_Density: 3, Mixed_Density: 2, High_Density: 1 }[v] || 0);
const riskMap = { High_Density: "Low", Mixed_Density: "Medium", Low_Density: "High" };
const label = v => riskMap[v] || "Not Verified";

async function dashboardView(fraud, satellite, sortCol = "risk_score", sortAsc = false) {
  // ---- loadSchools() exact param building ----
  const p = new URLSearchParams();
  p.set("skip", 0); p.set("limit", 25);
  if (fraud === "flagged_only") p.set("fraud_flag", "True");
  const sorts = [];
  if (fraud === "flagged_first") sorts.push("flagged_first");
  if (satellite === "high_first") sorts.push("high_first");
  if (sorts.length) p.set("sort", sorts.join(","));
  if (satellite && satellite !== "high_first") p.set("satellite", satellite);

  const res = await fetch(`${API}/schools?${p.toString()}`);
  const { items, total } = await res.json();

  // ---- renderTable() exact client sort ----
  const sorted = [...items].sort((a, b) => {
    if (fraud === "flagged_first") {
      const fa = a.Qwen_Fraud_Flag === "True" ? 1 : 0;
      const fb = b.Qwen_Fraud_Flag === "True" ? 1 : 0;
      if (fa !== fb) return fb - fa;
    }
    if (satellite === "high_first") {
      const sa = satRank(a.Satellite_Density_Flag), sb = satRank(b.Satellite_Density_Flag);
      if (sa !== sb) return sb - sa;
    }
    let va = a[sortCol], vb = b[sortCol];
    if (sortCol === "Satellite_Density_Flag") {
      const ra = satRank(va), rb = satRank(vb);
      return sortAsc ? ra - rb : rb - ra;
    }
    if (va == null) return 1; if (vb == null) return -1;
    if (typeof va === "number") return sortAsc ? va - vb : vb - va;
    return 0;
  });
  return { total, rows: sorted };
}

(async () => {
  console.log("=== SCENARIO 1: user selects 'High First' (default sort state) ===");
  let v = await dashboardView("", "high_first");
  console.log(`total=${v.total}, first 10 badges:`, v.rows.slice(0, 10).map(s => label(s.Satellite_Density_Flag)).join(", "));
  console.log("first row:", v.rows[0].School_Name, "| score", v.rows[0].risk_score);

  console.log("\n=== SCENARIO 2: 'High First' + user then clicks Satellite Risk column header ===");
  v = await dashboardView("", "high_first", "Satellite_Density_Flag", false);
  console.log("first 10 badges:", v.rows.slice(0, 10).map(s => label(s.Satellite_Density_Flag)).join(", "));

  console.log("\n=== SCENARIO 3: user selects 'High Only' ===");
  v = await dashboardView("", "High");
  console.log(`total=${v.total} (expect 6)`);
  v.rows.forEach(s => console.log(`   ${label(s.Satellite_Density_Flag)} | score ${String(s.risk_score).padEnd(6)} | ${s.School_Name.slice(0, 42)}`));

  console.log("\n=== SCENARIO 4: 'Flagged First' + 'High First' together ===");
  v = await dashboardView("flagged_first", "high_first");
  console.log(`total=${v.total}, first 10:`, v.rows.slice(0, 10).map(s =>
    `${s.Qwen_Fraud_Flag === "True" ? "F" : "-"}${label(s.Satellite_Density_Flag)[0]}`).join(" "));
  console.log("(F=flagged, H/M/L/N=satellite risk first letter)");

  console.log("\n=== SCENARIO 5: 'Not Verified' filter ===");
  v = await dashboardView("", "NotVerified");
  const badgeSet = new Set(v.rows.slice(0, 25).map(s => label(s.Satellite_Density_Flag)));
  console.log(`total=${v.total} (expect 53), badges on page 1:`, [...badgeSet].join(", "));

  console.log("\n=== SCENARIO 6: default view (no filter) still risk-score desc ===");
  v = await dashboardView("", "");
  console.log("first 5 scores:", v.rows.slice(0, 5).map(s => s.risk_score).join(", "));
})();
