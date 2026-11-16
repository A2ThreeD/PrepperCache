/*
 * PrepperCache
 * Copyright (c) 2026 Aaron Morris / A2ThreeD.
 * Shared progress panel for the offline portal.
 */
(() => {
  const start = () => {
    if (window.top !== window || document.getElementById("preppercache-job-status")) return;
    const host = document.createElement("div");
    host.id = "preppercache-job-status";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host {
          position: fixed; inset: auto max(1rem, env(safe-area-inset-right)) max(1rem, env(safe-area-inset-bottom)) auto;
          width: min(31rem, calc(100vw - 2rem)); z-index: 900;
          font: 0.875rem/1.4 system-ui, sans-serif; pointer-events: none;
        }
        aside {
          box-sizing: border-box; max-height: 37vh; overflow: auto; padding: .75rem 1rem;
          background: var(--bg-card, #ffffff); color: var(--text-primary, #172b2d); border: 1px solid var(--border-primary, #8da9a5);
          border-radius: .65rem; box-shadow: 0 .4rem 1.4rem rgba(0, 0, 0,.45);
          visibility: hidden; opacity: 0; transform: translateY(110%);
          transition: transform .2s, opacity .2s, visibility .2s;
        }
        aside.open { visibility: visible; opacity: 1; transform: translateY(0); pointer-events: auto; }
        section + section { margin-top: .65rem; padding-top: .65rem; border-top: 1px solid var(--border-subtle, #a9bfbb); }
        header { display: flex; align-items: baseline; justify-content: space-between; gap: .75rem; }
        section[data-problem] strong { color: var(--status-error, #8a2828); }
        a { color: var(--accent-primary, #006e68); }
        .notice-actions { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .9rem; margin-top: .4rem; }
        .dismiss { border: 0; background: transparent; color: inherit; font: inherit; text-decoration: underline; cursor: pointer; padding: .15rem 0; }
        p { margin: .2rem 0; overflow-wrap: anywhere; }
        progress { display: block; width: 100%; height: .55rem; accent-color: var(--accent-primary, #006e68); margin-top: .35rem; }
        @media (prefers-reduced-motion: reduce) { aside { transition: none; } }
      </style>
      <aside aria-label="PrepperCache status"></aside>
    `;
    document.body.append(host);
    const panel = root.querySelector("aside");
    const cacheKey = "preppercache-job-status-v1";
    const endpoints = ["/api/apply", "/api/maps/sync"];
    const jobs = [null, null];
    let checking = false;
    let notice = null;
    let noticeTimer;
    let storageJob = {};

    function updateStorageJob(job) {
      if (JSON.stringify(storageJob) !== JSON.stringify(job)) {
        storageJob = job;
        document.dispatchEvent(new CustomEvent("preppercache:storage-progress", { detail: job }));
      }
      render();
    }

    function storageJobView() {
      const { status, device, step, partition, finished_at: finishedAt } = storageJob;
      if (!status || (status !== "running" && Date.now() - Date.parse(finishedAt) > 20000)) return null;
      const running = status === "running";
      const failed = status === "failed";
      const href = partition ? `/admin/#files?device=${encodeURIComponent(partition)}`
        : `/admin/#storage?device=${encodeURIComponent(device)}`;
      return {
        title: running ? "Preparing USB drive" : failed ? "Drive preparation failed" : "USB drive ready",
        detail: step, href, busy: running, problem: failed, linkLabel: partition ? "Browse drive" : "Storage",
      };
    }
    const usbNotices = new Map();
    let knownUsbDrives = null;
    try {
      const saved = JSON.parse(sessionStorage.getItem("preppercache-usb-drives"));
      if (Array.isArray(saved)) knownUsbDrives = new Set(saved);
    } catch (_) {}

    function updateUsbDrives(volumes) {
      const drives = new Map();
      for (const volume of volumes) {
        if (volume.transport !== "usb" || !volume.path) continue;
        const key = volume.parent_path || volume.path;
        if (!drives.has(key)) drives.set(key, []);
        drives.get(key).push(volume);
      }
      for (const key of usbNotices.keys()) {
        if (!drives.has(key)) usbNotices.delete(key);
      }
      for (const [key, volumes] of drives) {
        if (knownUsbDrives?.has(key) && !usbNotices.has(key)) continue;
        const disk = volumes.find((volume) => !volume.is_partition);
        const partitions = volumes.filter((volume) => volume.is_partition);
        const choices = partitions.length ? partitions : volumes;
        const name = disk?.display_name || choices[0].display_name || key;
        const ready = choices.some((volume) => volume.preppercache_layout?.maps && volume.preppercache_layout?.library);
        const actions = [];
        let detail = `${name} has PrepperCache library and maps folders.`;
        if (!ready) {
          detail = disk?.can_prepare
            ? `${name} can be set up for library and maps use. Preparing erases all partitions on the drive.`
            : `${name} cannot be prepared by PrepperCache.`;
          actions.push({
            href: `/admin/#storage?device=${encodeURIComponent(key)}`,
            label: disk?.can_prepare ? "Set up drive" : "Storage settings",
          });
        }
        for (const volume of choices) {
          if (!volume.mounted && !volume.can_mount) continue;
          const label = choices.length > 1 ? `Browse ${volume.label || volume.path}` : "Browse drive";
          actions.push({ href: `/admin/#files?device=${encodeURIComponent(volume.path)}`, label });
        }
        usbNotices.set(key, { title: "USB drive inserted", detail, actions, usbKey: key });
      }
      knownUsbDrives = new Set(drives.keys());
      document.dispatchEvent(new CustomEvent("preppercache:usb-presence", { detail: drives.size > 0 }));
      try { sessionStorage.setItem("preppercache-usb-drives", JSON.stringify([...knownUsbDrives])); } catch (_) {}
      render();
    }

    try {
      const saved = JSON.parse(sessionStorage.getItem(cacheKey));
      if (saved && Date.now() - saved.at < 20000) jobs.splice(0, jobs.length, ...saved.jobs);
    } catch (_) {}

    function displayJob(state, map) {
      if (!state) return null;
      const indexWarning = !map && state.indexWarning;
      const problem = state.status === "failed" || (state.status === "succeeded" && (state.download_failed > 0 || indexWarning));
      if (state.status !== "running" && !(problem && Date.now() - Date.parse(state.finished_at) < 45000)) return null;
      const setup = !map && state.action === "full";
      const title = map ? "Offline maps" : setup ? "PrepperCache installation" : "Offline content";
      if (problem) {
        const failed = Number(state.download_failed);
        const summary = indexWarning || state.error || `Finished with ${failed} failed download${failed === 1 ? "" : "s"}.`;
        const filenames = state.failed_download_names || [];
        const files = filenames.slice(0, 2).join(", ");
        const more = filenames.length > 2 ? ` and ${filenames.length - 2} more` : "";
        return { title, detail: files ? `${summary} ${files}${more}` : summary, problem: true, href: map ? "/admin/#maps" : "/admin/#content-log" };
      }
      const percent = map && state.total_files && state.current_index && Number.isFinite(state.current_percent)
        ? Math.floor(((state.current_index - 1) * 100 + state.current_percent) / state.total_files)
        : state.progress_percent;
      const files = (state.active_downloads || []).map((file) => file.filename);
      const count = state.downloads_active && state.download_total
        ? `${state.download_current || 0} of ${state.download_total} files processed` : "";
      const mapName = state.current_label || state.current_file;
      const detail = map ? (mapName ? `${mapName}${state.total_files ? ` · map ${state.current_index} of ${state.total_files}` : ""}` : "Preparing maps…")
        : [state.step || "Preparing…", count, files.length ? files.join(", ") : state.downloads_active ? state.current_file : ""].filter(Boolean).join(" · ");
      return { title, detail, percent };
    }

    const mapCanvas = document.getElementById("map");
    let lastView = "";
    function render() {
      const mapProblem = mapCanvas?.dataset.problem;
      const views = jobs.map((job, index) => displayJob(job, index === 1));
      views.push(mapProblem ? { title: "Offline map problem", detail: mapProblem, problem: true, mapSettings: true } : null);
      views.push(notice, storageJobView(), ...usbNotices.values());
      const serialized = JSON.stringify(views);
      if (lastView === serialized) return;
      lastView = serialized;
      const sections = views.map((view, index) => {
        if (!view) return null;
        const section = document.createElement("section");
        if (view.problem) {
          section.dataset.problem = "";
          section.setAttribute("role", "alert");
        } else if (view.href || view.actions) {
          section.setAttribute("role", "status");
        }
        const header = document.createElement("header");
        const title = document.createElement("strong");
        title.textContent = view.title;
        const link = document.createElement("a");
        const localHref = (href) => location.pathname === "/admin/" && href.startsWith("/admin/#") ? href.slice(7) : href;
        link.href = localHref(view.href || (view.mapSettings || index === 1 ? "/admin/#maps" : "/admin/#content"));
        link.textContent = view.linkLabel || (view.mapSettings ? "Map Settings" : "Details");
        const detail = document.createElement("p");
        detail.textContent = view.detail;
        header.append(title);
        if (!view.actions) header.append(link);
        section.append(header, detail);
        if (view.actions) {
          const actions = document.createElement("div");
          actions.className = "notice-actions";
          for (const action of view.actions) {
            const anchor = document.createElement("a");
            anchor.href = localHref(action.href);
            anchor.textContent = action.label;
            anchor.addEventListener("click", () => {
              usbNotices.delete(view.usbKey);
              render();
            });
            actions.append(anchor);
          }
          const dismiss = document.createElement("button");
          dismiss.type = "button";
          dismiss.className = "dismiss";
          dismiss.textContent = "Dismiss";
          dismiss.onclick = () => {
            usbNotices.delete(view.usbKey);
            render();
          };
          actions.append(dismiss);
          section.append(actions);
        }
        if ("percent" in view || view.busy) {
          const meter = document.createElement("progress");
          meter.max = 100;
          meter.setAttribute("aria-label", view.title);
          if (Number.isFinite(view.percent)) {
            meter.value = Math.max(0, Math.min(100, view.percent));
            title.textContent += ` · ${meter.value}%`;
          }
          section.append(meter);
        }
        return section;
      }).filter(Boolean);
      if (sections.length) panel.replaceChildren(...sections);
      panel.classList.toggle("open", Boolean(sections.length));
      panel.inert = !sections.length;
    }

    async function refresh() {
      if (checking || document.hidden) return;
      checking = true;
      try {
        await Promise.allSettled([...endpoints, "/api/system/storage/volumes", "/api/system/storage/prepare"].map(async (url, index) => {
          const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(4500) });
          if (!response.ok) return;
          const { log_tail: log, ...state } = await response.json();
          if (url === "/api/system/storage/prepare") {
            updateStorageJob(state);
            return;
          }
          if (url === "/api/system/storage/volumes") {
            if (Array.isArray(state.volumes)) updateUsbDrives(state.volumes);
            return;
          }
          if (index === 0) {
            state.indexWarning = (log || []).findLast((line) => line.startsWith("Kiwix could not index: "));
          }
          jobs[index] = displayJob(state, index === 1) ? state : null;
          render();
        }));
        try { sessionStorage.setItem(cacheKey, JSON.stringify({ at: Date.now(), jobs })); } catch (_) {}
      } finally {
        checking = false;
        render();
      }
    }

    document.addEventListener("preppercache:storage-started", ({ detail }) => updateStorageJob(detail));

    document.addEventListener("preppercache:notice", ({ detail }) => {
      clearTimeout(noticeTimer);
      notice = detail;
      render();
      if (!notice.busy) {
        noticeTimer = window.setTimeout(() => {
          notice = null;
          render();
        }, notice.problem ? 12000 : 6500);
      }
    });

    if (mapCanvas) {
      new MutationObserver(render).observe(mapCanvas, { attributeFilter: ["data-problem"] });
    }
    render();
    refresh();
    window.setInterval(refresh, 2500);
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", refresh);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
