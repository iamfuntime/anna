// ANNA Dashboard — minimal vanilla JS.
//
// Single responsibility: the .env masked-input reveal toggle.
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
