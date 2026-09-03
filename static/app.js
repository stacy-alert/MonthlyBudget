function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function badgeClass(verdict) {
  switch (verdict) {
    case "Recommended": return "badge-recommended";
    case "Consider": return "badge-consider";
    case "Avoid": return "badge-avoid";
    default: return "badge-error";
  }
}

function passFailSpan(pass) {
  if (pass === undefined || pass === null) return "";
  return pass ? '<span class="pass">PASS</span>' : '<span class="fail">FAIL</span>';
}

function fmtVolume(v) {
  if (v === null || v === undefined) return "-";
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(0) + "K";
  return String(Math.round(v));
}

function fmtPct(v) {
  if (v === null || v === undefined) return "-";
  return v.toFixed(2) + "%";
}

// --- Scan ---

const scanBtn = document.getElementById("scan-btn");
const scanDateInput = document.getElementById("scan-date");
const minMcapInput = document.getElementById("min-mcap");
const progressWrap = document.getElementById("scan-progress");
const progressFill = document.getElementById("progress-fill");
const progressLabel = document.getElementById("progress-label");
const scanError = document.getElementById("scan-error");
const resultsBody = document.getElementById("results-body");
const resultsCount = document.getElementById("results-count");

const today = new Date();
scanDateInput.value = today.toISOString().slice(0, 10);

let pollTimer = null;

function setScanBusy(busy) {
  scanBtn.disabled = busy;
  scanBtn.textContent = busy ? "Scanning..." : "Scan Today's Earnings";
}

async function startScan() {
  clearInterval(pollTimer);
  scanError.classList.add("hidden");
  progressWrap.classList.remove("hidden");
  progressFill.style.width = "0%";
  progressLabel.textContent = "Loading earnings calendar...";
  setScanBusy(true);
  resultsBody.innerHTML = '<tr><td colspan="8" class="muted center">Scanning...</td></tr>';
  resultsCount.textContent = "";

  let resp;
  try {
    resp = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date: scanDateInput.value || null,
        min_market_cap: minMcapInput.value ? Number(minMcapInput.value) : null,
      }),
    });
  } catch (e) {
    showScanError("Network error starting scan: " + e.message);
    return;
  }

  const data = await resp.json();
  if (!resp.ok) {
    showScanError(data.error || "Failed to start scan.");
    return;
  }

  if (data.watchlist_size === 0) {
    progressWrap.classList.add("hidden");
    setScanBusy(false);
    resultsBody.innerHTML = '<tr><td colspan="8" class="muted center">No earnings events found for that date/filter.</td></tr>';
    return;
  }

  pollScan(data.job_id);
}

function pollScan(jobId) {
  pollTimer = setInterval(async () => {
    let resp;
    try {
      resp = await fetch(`/api/scan/${jobId}`);
    } catch (e) {
      return; // transient - try again next tick
    }
    if (!resp.ok) {
      clearInterval(pollTimer);
      showScanError("Lost track of scan job.");
      return;
    }
    const job = await resp.json();
    const { done, total } = job.progress || { done: 0, total: 0 };
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    progressFill.style.width = pct + "%";
    progressLabel.textContent = `${done} / ${total} tickers`;

    if (job.status === "done") {
      clearInterval(pollTimer);
      setScanBusy(false);
      progressWrap.classList.add("hidden");
      renderResults(job.results || []);
    } else if (job.status === "error") {
      clearInterval(pollTimer);
      showScanError(job.error || "Scan failed.");
    }
  }, 1000);
}

function showScanError(msg) {
  clearInterval(pollTimer);
  setScanBusy(false);
  progressWrap.classList.add("hidden");
  scanError.textContent = msg;
  scanError.classList.remove("hidden");
}

function renderResults(results) {
  resultsCount.textContent = `(${results.length})`;
  if (results.length === 0) {
    resultsBody.innerHTML = '<tr><td colspan="8" class="muted center">No results.</td></tr>';
    return;
  }

  resultsBody.innerHTML = results
    .map((r) => {
      if (r.error) {
        return `<tr>
          <td>${escapeHtml(r.ticker)}</td>
          <td><span class="badge badge-error">Error</span></td>
          <td colspan="6" class="muted">${escapeHtml(r.error)}</td>
        </tr>`;
      }
      return `<tr>
        <td><strong>${escapeHtml(r.ticker)}</strong>${r.name ? `<br><span class="muted">${escapeHtml(r.name)}</span>` : ""}</td>
        <td><span class="badge ${badgeClass(r.verdict)}">${escapeHtml(r.verdict)}</span></td>
        <td class="muted">${escapeHtml(r.session || "-")}</td>
        <td>${fmtPct(r.expected_move_pct)}</td>
        <td>${fmtVolume(r.avg_volume)} ${passFailSpan(r.avg_volume_pass)}</td>
        <td>${r.iv30_rv30 ?? "-"} ${passFailSpan(r.iv30_rv30_pass)}</td>
        <td>${r.ts_slope_0_45 ?? "-"} ${passFailSpan(r.ts_slope_pass)}</td>
        <td>${r.underlying_price !== undefined ? "$" + r.underlying_price : "-"}</td>
      </tr>`;
    })
    .join("");
}

scanBtn.addEventListener("click", startScan);

// --- Single ticker lookup ---

const tickerInput = document.getElementById("ticker-input");
const lookupBtn = document.getElementById("lookup-btn");
const singleResult = document.getElementById("single-result");

async function lookupTicker() {
  const symbol = tickerInput.value.trim();
  if (!symbol) return;

  singleResult.innerHTML = '<p class="muted">Checking...</p>';
  lookupBtn.disabled = true;

  let resp, data;
  try {
    resp = await fetch(`/api/ticker/${encodeURIComponent(symbol)}`);
    data = await resp.json();
  } catch (e) {
    singleResult.innerHTML = `<p class="error">Network error: ${escapeHtml(e.message)}</p>`;
    lookupBtn.disabled = false;
    return;
  }
  lookupBtn.disabled = false;

  if (!resp.ok || data.error) {
    singleResult.innerHTML = `<p class="error">${escapeHtml(data.error || "Lookup failed.")}</p>`;
    return;
  }

  singleResult.innerHTML = `
    <div class="single-card">
      <div><strong>${escapeHtml(data.ticker)}</strong> - <span class="badge ${badgeClass(data.verdict)}">${escapeHtml(data.verdict)}</span></div>
      <div>Expected move: ${fmtPct(data.expected_move_pct)}</div>
      <div>Avg volume (30d): ${fmtVolume(data.avg_volume)} ${passFailSpan(data.avg_volume_pass)}</div>
      <div>IV30/RV30: ${data.iv30_rv30} ${passFailSpan(data.iv30_rv30_pass)}</div>
      <div>Term structure slope (0-45): ${data.ts_slope_0_45} ${passFailSpan(data.ts_slope_pass)}</div>
      <div>Underlying price: $${data.underlying_price}</div>
    </div>
  `;
}

lookupBtn.addEventListener("click", lookupTicker);
tickerInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") lookupTicker();
});
