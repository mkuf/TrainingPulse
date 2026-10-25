// Keeps the status page's live region current without reloading the page.
(function () {
    const RUNNING_MS = 3000;
    const IDLE_MS = 30000;
    // After a button press, poll quickly until the new sync shows up.
    const KICKOFF_MS = 10000;

    let timer = null;
    let fastUntil = 0;

    function liveRegion() {
        return document.getElementById("live");
    }

    function schedule() {
        clearTimeout(timer);
        const live = liveRegion();
        const running = live && live.dataset.running === "true";
        const fast = running || Date.now() < fastUntil;
        timer = setTimeout(refresh, fast ? RUNNING_MS : IDLE_MS);
    }

    async function refresh() {
        try {
            const res = await fetch("/partials/live", { cache: "no-store" });
            const live = liveRegion();
            if (res.ok && live) live.outerHTML = await res.text();
        } catch (e) {
            // Offline or restarting; try again on the next tick.
        }
        schedule();
    }

    document.addEventListener("submit", async function (event) {
        const form = event.target;
        if (!form.matches("[data-sync-action]")) return;
        event.preventDefault();
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;

        const button = form.querySelector("button");
        if (button) button.textContent = "Starting…";
        try {
            await fetch(form.action, { method: "POST", redirect: "manual" });
        } catch (e) {
            // The next refresh shows whatever state the server is in.
        }
        const details = form.closest("details");
        if (details) {
            details.open = false;
            if (button) button.textContent = "Started";
        }
        fastUntil = Date.now() + KICKOFF_MS;
        refresh();
    });

    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "visible") refresh();
    });

    schedule();
})();
