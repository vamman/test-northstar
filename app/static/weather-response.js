'use strict';
(() => {
  const panel = document.createElement('section');
  panel.className = 'panel';
  panel.setAttribute('aria-label', 'Weather travel response');
  panel.style.cssText = 'padding:16px;margin-bottom:16px';
  document.getElementById('view-overview').prepend(panel);
  let timer;
  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch('/api/environment', {signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('unavailable');
      const observation = await response.json(), o = observation.weather_response_outcomes;
      const title = document.createElement('strong'), detail = document.createElement('p'), stamp = document.createElement('small');
      title.textContent = 'Weather travel response';
      detail.textContent = o ? `${o.suppressed_individual_trips} individual departures suppressed · ${o.suppressed_road_vehicles} road vehicles suppressed · ${o.suppressed_office_commutes} named office commutes suppressed in latest advance` : 'Awaiting a worker-applied weather observation.';
      stamp.textContent = `${observation.application_status || 'not applied'} · ${observation.applied_simulated_at || 'No observation yet'} · essential and already-started journeys are retained`;
      panel.replaceChildren(title, detail, stamp);
    } catch (_) { panel.textContent = 'Weather travel response is temporarily unavailable.'; }
    clearTimeout(timer); timer = setTimeout(refresh, 15000);
  }
  document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) refresh(); });
  refresh();
})();
