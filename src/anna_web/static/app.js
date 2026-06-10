// ANNA Dashboard — minimal vanilla JS.
//
// Two responsibilities:
//   1. The .env masked-input reveal toggle.
//   2. The light/dark theme toggle (MC-01).
//
// Reveal toggle: the .env masked-input show/hide.
// On click of any .reveal-toggle button, flip the sibling input's
// type between "password" and "text". Fire a fetch() to
// /env/<key>/reveal to load the actual value (server returns the raw
// value only on this endpoint; the rest of the surface uses an empty
// value plus data-has-value="true").
//
// Event delegation so dynamically-added rows from subtask 8's "extra
// rows" UX work without rebinding.

document.addEventListener("click", function (event) {
  var btn = event.target.closest(".reveal-toggle");
  if (!btn) {
    return;
  }
  event.preventDefault();

  var key = btn.dataset.envKey;
  if (!key) {
    return;
  }

  var input = document.querySelector('[data-env-input="' + key + '"]');
  if (!input) {
    return;
  }

  var isMasked = input.type === "password";
  input.type = isMasked ? "text" : "password";
  btn.textContent = isMasked ? "hide" : "show";

  if (!isMasked) {
    // Re-mask: clear the in-DOM value so the secret doesn't linger.
    input.value = "";
    return;
  }

  // Fetch the real value from the reveal endpoint (subtask 8 lands it).
  fetch("/env/" + encodeURIComponent(key) + "/reveal")
    .then(function (response) {
      if (!response.ok) {
        // 404 is expected until subtask 8 ships; log + leave empty.
        console.warn("reveal endpoint returned", response.status, "for", key);
        return "";
      }
      return response.text();
    })
    .then(function (text) {
      input.value = text;
    })
    .catch(function (err) {
      console.warn("reveal fetch failed for", key, err);
    });
});

// Theme toggle (MC-01). The inline head script in base.html seeds
// data-theme on <html> before first paint; this handler only flips the
// attribute and persists the choice. Event delegation matches the
// reveal-toggle pattern above, so the control keeps working if a future
// nav redesign re-renders it via HTMX.
document.addEventListener("click", function (event) {
  var btn = event.target.closest('[data-action="toggle-theme"]');
  if (!btn) {
    return;
  }
  event.preventDefault();

  var root = document.documentElement;
  var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
  root.setAttribute("data-theme", next);
  try {
    localStorage.setItem("anna-theme", next);
  } catch (err) {
    // Storage unavailable (private mode / sandbox): theme still flips
    // for this page view, it just won't persist.
    console.warn("could not persist theme preference", err);
  }
});
