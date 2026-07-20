const exportForms = document.querySelectorAll(".export-form");
const exportButtons = document.querySelectorAll(".export-button");

let downloadPoll = null;
let downloadTimeout = null;

function setExportLoading(activeButton) {
  exportButtons.forEach((button) => {
    button.disabled = true;

    if (button === activeButton) {
      button.classList.add("is-loading");
      const label = button.querySelector(".button-label");
      if (label) {
        label.dataset.originalLabel = label.textContent;
        label.textContent = button.dataset.loadingLabel || "Preparing";
      }
    }
  });
}

function resetExportLoading() {
  window.clearInterval(downloadPoll);
  window.clearTimeout(downloadTimeout);
  downloadPoll = null;
  downloadTimeout = null;

  exportButtons.forEach((button) => {
    button.disabled = false;
    button.classList.remove("is-loading");

    const label = button.querySelector(".button-label");
    if (label?.dataset.originalLabel) {
      label.textContent = label.dataset.originalLabel;
      delete label.dataset.originalLabel;
    }
  });
}

function hasDownloadToken(token) {
  return document.cookie
    .split(";")
    .map((cookie) => cookie.trim())
    .some((cookie) => cookie === `tubenow_download=${token}`);
}

function clearDownloadToken() {
  document.cookie = "tubenow_download=; Max-Age=0; path=/; SameSite=Lax";
}

function watchForDownloadStart(token) {
  downloadPoll = window.setInterval(() => {
    if (hasDownloadToken(token)) {
      clearDownloadToken();
      resetExportLoading();
    }
  }, 250);

  downloadTimeout = window.setTimeout(resetExportLoading, 120000);
}

exportForms.forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector(".export-button");
    const token = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    let tokenInput = form.querySelector('input[name="download_token"]');

    if (!tokenInput) {
      tokenInput = document.createElement("input");
      tokenInput.type = "hidden";
      tokenInput.name = "download_token";
      form.appendChild(tokenInput);
    }

    tokenInput.value = token;
    setExportLoading(button);
    watchForDownloadStart(token);
  });
});

window.addEventListener("pageshow", resetExportLoading);
