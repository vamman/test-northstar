/* Shared Northstar clock status. Poll persisted state; never invent local time. */
(() => {
  let panel;
  const format = value => new Date(value).toLocaleString('en-CA', {
    timeZone:'America/Moncton',year:'numeric',month:'short',day:'numeric',
    hour:'2-digit',minute:'2-digit',hour12:false
  });
  async function refresh() {
    try {
      const response = await fetch('/api/clock', {cache:'no-store'});
      const data = await response.json();
      if (data.mode !== 'shared') return;
      if (!panel) {
        panel = document.createElement('aside');
        panel.setAttribute('aria-label','Shared Northstar clock');
        panel.style.cssText='position:relative;z-index:1000;padding:9px 18px;background:#142e3e;color:#edf8ff;font:13px/1.5 system-ui;border-bottom:1px solid #486577;display:flex;gap:12px;align-items:center;flex-wrap:wrap';
        document.body.prepend(panel);
      }
      const sidebar = document.querySelector('.sidebar');
      const side = sidebar?.getBoundingClientRect();
      panel.style.marginLeft = side && side.left >= -1 && side.width < innerWidth * .6 ? `${side.right}px` : '0';
      if (!response.ok) { panel.textContent='Northstar clock held · '+(data.error || 'Coordinator unavailable'); return; }
      const clock = data.authority;
      const status = clock.paused ? 'Paused' : Object.keys(clock.waiting_for || {}).length ? 'Waiting for services' : 'Running';
      panel.replaceChildren();
      const text = document.createElement('span');
      text.textContent=`Northstar · ${format(clock.target_at)} Atlantic · ${status} · ${data.caught_up ? 'This service is caught up' : `Processing (${Math.max(0,data.lag_seconds)}s remaining)`}`;
      const link = document.createElement('a');
      link.href='/api/clock'; link.target='_blank'; link.rel='noopener'; link.textContent='Clock details';link.style.color='#93dfef';
      panel.append(text,link);
      panel.title=`Requested pace: one simulated minute per ${clock.wall_seconds_per_minute} real seconds. All services must finish before the next minute. Local time controls are managed centrally.`;
      const statusId = {office:'simulation-status', clinics:'sim-running', environment:'worker-status'}[data.peer];
      const localStatus = statusId && document.getElementById(statusId);
      if(localStatus) localStatus.textContent = `Shared clock · ${status}`;
      // Disable only recognizable time widgets; scenario and operational controls remain available.
      document.querySelectorAll('button,select,input').forEach(el => {
        const action = el.dataset.action || el.dataset.control || '';
        const id = el.id || '';
        if (['pause','resume','speed','step','advance','jump','reset','start_workday'].includes(action) || /^(pause|resume|speed|sim-speed|speed-select|pause-btn|resume-btn|step-btn|advance-btn|toggle-running|toggle-pause|start-workday|pause-control|simulation-speed|toggle-clock|play-pause|control-run|control-run-toggle|step)$/.test(id)) {
          el.disabled=true;el.title='Managed by the shared Northstar clock';
        }
      });
    } catch (_) { if(panel)panel.textContent='Northstar clock status unavailable'; }
  }
  refresh(); setInterval(refresh,4000);
})();
