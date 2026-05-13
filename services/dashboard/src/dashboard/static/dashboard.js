// Dashboard client behaviour: Alpine.js component factories for the runs
// list and run-detail pages, plus the live "in-flight" elapsed-time chip
// shared by both. Loaded once via base.html so both pages get the same
// helpers without duplicating logic inline in the templates.

function readJsonScript(id, fallback) {
  const node = document.getElementById(id);
  if (!node) return fallback;
  try {
    return JSON.parse(node.textContent || 'null') ?? fallback;
  } catch (_) {
    return fallback;
  }
}

function formatElapsed(sinceIso) {
  if (!sinceIso) return '';
  const since = Date.parse(sinceIso);
  if (Number.isNaN(since)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - since) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function isStuck(sinceIso, thresholdSeconds) {
  if (!sinceIso || !thresholdSeconds) return false;
  const since = Date.parse(sinceIso);
  if (Number.isNaN(since)) return false;
  return (Date.now() - since) / 1000 > thresholdSeconds;
}

function runDetail(runId, initialCursor) {
  const POLL_MS = 2500;
  const TICK_MS = 1000;
  return {
    liveEvents: [],
    lastType: '',
    cursor: initialCursor || '',
    timer: null,
    ticker: null,
    progress: readJsonScript('initial-progress', null),
    elapsed: '',
    stuck: false,
    connect() {
      this.refreshElapsed();
      this.ticker = window.setInterval(() => this.refreshElapsed(), TICK_MS);
      this.poll();
      this.timer = window.setInterval(() => this.poll(), POLL_MS);
    },
    refreshElapsed() {
      if (!this.progress || !this.progress.since) {
        this.elapsed = '';
        this.stuck = false;
        return;
      }
      this.elapsed = formatElapsed(this.progress.since);
      this.stuck = isStuck(this.progress.since, this.progress.stuck_threshold_seconds);
    },
    async poll() {
      const qs = this.cursor ? `?since=${encodeURIComponent(this.cursor)}` : '';
      let resp;
      try {
        resp = await fetch(`/v1/runs/${encodeURIComponent(runId)}/events${qs}`);
      } catch (_) {
        return;
      }
      if (!resp.ok) return;
      const body = await resp.json();
      for (const ev of body.events || []) {
        this.liveEvents.push(ev);
        this.lastType = ev.type;
        if (ev.event_id > this.cursor) this.cursor = ev.event_id;
      }
      this.progress = body.progress || null;
      this.refreshElapsed();
      if (body.terminal) {
        this.progress = null;
        this.elapsed = '';
        this.stuck = false;
        if (this.timer !== null) {
          window.clearInterval(this.timer);
          this.timer = null;
        }
        if (this.ticker !== null) {
          window.clearInterval(this.ticker);
          this.ticker = null;
        }
      }
    },
    async deleteRun() {
      if (!window.confirm(`Delete run ${runId}? This permanently removes its events.`)) return;
      const resp = await fetch(`/v1/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
      if (resp.status === 204) {
        window.location.assign('/');
        return;
      }
      const text = await resp.text().catch(() => '');
      window.alert(`Could not delete ${runId}: ${resp.status} ${text}`);
    },
  };
}

function runsList() {
  const TICK_MS = 1000;
  return {
    filter: '',
    deleted: [],
    runsProgress: readJsonScript('initial-runs-progress', {}),
    liveChips: {},
    connect() {
      this.refreshChips();
      window.setInterval(() => this.refreshChips(), TICK_MS);
    },
    refreshChips() {
      const next = {};
      for (const [runId, prog] of Object.entries(this.runsProgress || {})) {
        if (!prog || !prog.since) continue;
        next[runId] = {
          elapsed: formatElapsed(prog.since),
          stuck: isStuck(prog.since, prog.stuck_threshold_seconds),
        };
      }
      this.liveChips = next;
    },
    async deleteRun(runId) {
      if (!window.confirm(`Delete run ${runId}? This permanently removes its events.`)) return;
      const resp = await fetch(`/v1/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' });
      if (resp.status === 204) {
        this.deleted.push(runId);
        return;
      }
      const text = await resp.text().catch(() => '');
      window.alert(`Could not delete ${runId}: ${resp.status} ${text}`);
    },
  };
}
