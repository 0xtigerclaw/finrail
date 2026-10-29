/* Progressive enhancements only: the server remains the source of truth. */
(() => {
  "use strict";
  document.documentElement.classList.add("js");

  let status = document.getElementById("ui-status");
  if (!status) {
    status = document.createElement("div");
    status.id = "ui-status";
    status.className = "sr-only";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    document.body.append(status);
  }
  function announce(message) {
    status.textContent = "";
    requestAnimationFrame(() => { status.textContent = message; });
  }

  // A drawer on small screens; the same navigation is always visible on desktop.
  const nav = document.getElementById("workspace-nav");
  const appColumn = document.querySelector(".app-column");
  const toggles = Array.from(document.querySelectorAll("[data-nav-toggle]"));
  const narrow = window.matchMedia("(max-width: 760px)");
  let opener = null;
  let backdrop = document.querySelector("[data-nav-backdrop]");
  let navOpen = false;
  if (nav && toggles.length) {
    if (!backdrop) {
      backdrop = document.createElement("button");
      backdrop.type = "button";
      backdrop.className = "nav-backdrop";
      backdrop.setAttribute("data-nav-backdrop", "");
      backdrop.setAttribute("aria-label", "Close navigation");
      backdrop.tabIndex = -1;
      backdrop.hidden = true;
      document.body.append(backdrop);
    }
    const focusable = () => Array.from(nav.querySelectorAll(
      "a[href], button:not(:disabled), summary, input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])"
    )).filter(element => element.getClientRects().length
      && !element.closest("[hidden], [inert]")
      && (!element.closest("details:not([open])") || element.tagName === "SUMMARY")
      && getComputedStyle(element).visibility === "visible");

    const setNav = (open, restoreFocus = true) => {
      const wasOpen = navOpen;
      navOpen = Boolean(open && narrow.matches);
      document.body.dataset.navOpen = String(navOpen);
      for (const toggle of toggles) toggle.setAttribute("aria-expanded", String(navOpen));
      backdrop.hidden = !navOpen;
      if (narrow.matches && !navOpen) {
        nav.setAttribute("inert", "");
        nav.setAttribute("aria-hidden", "true");
      } else {
        nav.removeAttribute("inert");
        nav.removeAttribute("aria-hidden");
      }
      if (navOpen && !wasOpen) {
        opener = toggles.includes(document.activeElement) ? document.activeElement : toggles[0];
        nav.setAttribute("role", "dialog");
        nav.setAttribute("aria-modal", "true");
        if (appColumn) appColumn.setAttribute("inert", "");
        const initialFocus = focusable()[0];
        if (initialFocus) initialFocus.focus({ preventScroll: true });
        requestAnimationFrame(() => {
          const first = focusable()[0];
          if (first) first.focus({ preventScroll: true });
        });
      } else if (!navOpen) {
        nav.removeAttribute("role");
        nav.removeAttribute("aria-modal");
        if (appColumn) appColumn.removeAttribute("inert");
        if (wasOpen && restoreFocus && opener instanceof HTMLElement) opener.focus({ preventScroll: true });
      }
    };
    // The drawer's visibility transition can precede its first focusable layout.
    nav.addEventListener("transitionend", event => {
      if (event.target === nav && navOpen && !nav.contains(document.activeElement)) {
        const first = focusable()[0];
        if (first) first.focus({ preventScroll: true });
      }
    });
    for (const toggle of toggles) {
      toggle.addEventListener("click", () => setNav(!navOpen));
    }
    backdrop.addEventListener("click", () => setNav(false));
    for (const close of document.querySelectorAll("[data-nav-close]")) {
      close.addEventListener("click", () => setNav(false));
    }
    nav.addEventListener("click", event => {
      if (event.target instanceof Element && event.target.closest("a[href]")) setNav(false, false);
    });
    document.addEventListener("keydown", event => {
      if (!navOpen) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setNav(false);
      } else if (event.key === "Tab") {
        const elements = focusable();
        if (!elements.length) return;
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (event.shiftKey && (document.activeElement === first || !nav.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !nav.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    narrow.addEventListener("change", () => setNav(false));
    setNav(false, false);
  }

  // Clipboard success is announced only after the browser confirms the write.
  const copyTimers = new WeakMap();
  document.addEventListener("click", async event => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest("[data-copy]");
    if (!(button instanceof HTMLButtonElement)) return;
    event.preventDefault();
    if (button.disabled) return;
    const value = button.getAttribute("data-copy");
    if (value === null) return;
    if (copyTimers.has(button)) clearTimeout(copyTimers.get(button));
    const original = button.dataset.copyOriginal || button.innerHTML;
    button.dataset.copyOriginal = original;
    button.disabled = true;
    button.textContent = "Copying…";
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value);
      button.textContent = "Copied";
      announce("Command copied to clipboard.");
    } catch {
      button.textContent = "Copy failed";
      announce("Could not copy the command. Select the command text and copy it manually.");
    } finally {
      button.disabled = false;
      copyTimers.set(button, setTimeout(() => {
        button.innerHTML = original;
        delete button.dataset.copyOriginal;
        copyTimers.delete(button);
      }, 1800));
    }
  });

  // Examples fill a real field only. The person still reviews and submits it.
  document.addEventListener("click", event => {
    if (!(event.target instanceof Element)) return;
    const example = event.target.closest("[data-fill-target][data-fill-value]");
    if (!example) return;
    const identifier = (example.getAttribute("data-fill-target") || "").replace(/^#/, "");
    if (!identifier) return;
    const field = document.getElementById(identifier) || document.querySelector(
      `input[name="${CSS.escape(identifier)}"], textarea[name="${CSS.escape(identifier)}"]`
    );
    if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return;
    if (field.disabled || field.readOnly || field.type === "hidden") return;
    event.preventDefault();
    field.value = example.getAttribute("data-fill-value") || "";
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.focus();
    announce("Example added. Review the drafting task before submitting.");
  });

  // Native validation runs before submit. Mark only accepted POST submissions busy.
  const submitted = new Map();
  document.addEventListener("submit", event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (event.defaultPrevented || !form.checkValidity()) return;
    if (submitted.has(form)) {
      event.preventDefault();
      return;
    }
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    const buttons = Array.from(form.elements).filter(element =>
      (element instanceof HTMLButtonElement && element.type === "submit") ||
      (element instanceof HTMLInputElement && ["submit", "image"].includes(element.type))
    );
    const saved = buttons.map(button => ({
      button,
      disabled: button.disabled,
      html: button instanceof HTMLButtonElement ? button.innerHTML : button.value
    }));
    // Disabled submitters no longer contribute their name/value to native FormData.
    const submitter = event.submitter;
    if (submitter && submitter.name) {
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = submitter.name;
      field.value = submitter.value;
      field.setAttribute("data-busy-submitter", "");
      form.append(field);
    }
    submitted.set(form, saved);
    form.setAttribute("aria-busy", "true");
    for (const { button, disabled } of saved) {
      if (disabled) continue;
      button.disabled = true;
      if (button instanceof HTMLButtonElement) button.textContent = "Working…";
      else button.value = "Working…";
    }
    announce("Your request is being processed. Please wait.");
  });
  window.addEventListener("pageshow", () => {
    for (const [form, buttons] of submitted) {
      form.removeAttribute("aria-busy");
      for (const { button, disabled, html } of buttons) {
        button.disabled = disabled;
        if (button instanceof HTMLButtonElement) button.innerHTML = html;
        else button.value = html;
      }
      for (const field of form.querySelectorAll("[data-busy-submitter]")) field.remove();
    }
    submitted.clear();
  });

  // A pending result may point at a safe GET detail route. Never reload a POST URL.
  const pending = document.querySelector("[data-run-refresh][data-refresh-url]");
  let refreshTimer = null;
  if (pending) {
    let refreshURL;
    try { refreshURL = new URL(pending.getAttribute("data-refresh-url"), location.href); }
    catch { refreshURL = null; }
    if (refreshURL && refreshURL.origin === location.origin && /^https?:$/.test(refreshURL.protocol)) {
      const refresh = () => {
        refreshTimer = setTimeout(() => {
          if (document.hidden) { refresh(); return; }
          if (document.querySelector("form[aria-busy='true']")) return;
          location.replace(refreshURL.href);
        }, 4000);
      };
      refresh();
    }
  }
  window.addEventListener("pagehide", () => {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
  });

  // Ask: Enter sends, Shift+Enter adds a line, as in any chat.
  const composer = document.querySelector(".composer textarea");
  if (composer) {
    composer.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && composer.value.trim().length >= 3) {
        event.preventDefault();
        composer.form.requestSubmit();
      }
    });
  }
})();
