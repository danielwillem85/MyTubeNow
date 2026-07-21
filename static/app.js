const exportForms = document.querySelectorAll(".export-form");
const exportButtons = document.querySelectorAll(".export-button");
const urlForm = document.querySelector(".url-form");
const authModal = document.querySelector("#auth-modal");
const authTabs = document.querySelectorAll("[data-auth-tab]");
const authPanels = document.querySelectorAll("[data-auth-panel]");
const signupForms = document.querySelectorAll("[data-signup-form]");
const emailOptInModal = document.querySelector("#email-opt-in-modal");
const proModal = document.querySelector("#pro-modal");
const loginLaunchers = document.querySelectorAll("[data-login-launcher]");

let downloadPoll = null;
let downloadTimeout = null;
let pendingSignupForm = null;

function selectAuthTab(tabName) {
  authTabs.forEach((tab) => {
    const isActive = tab.dataset.authTab === tabName;
    tab.classList.toggle("is-active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
    tab.tabIndex = isActive ? 0 : -1;
  });

  authPanels.forEach((panel) => {
    panel.hidden = panel.dataset.authPanel !== tabName;
  });
}

function openAuthModal(tabName = "login") {
  if (!authModal) {
    return;
  }

  const pendingUrl = document.querySelector("#url")?.value.trim() || "";
  authModal.querySelectorAll('input[name="pending_url"]').forEach((input) => {
    input.value = pendingUrl;
  });
  selectAuthTab(tabName);

  if (!authModal.open) {
    authModal.showModal();
  }
}

authTabs.forEach((tab) => {
  tab.addEventListener("click", () => selectAuthTab(tab.dataset.authTab));
});

loginLaunchers.forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    openAuthModal("login");
  });
});

authModal?.querySelector(".modal-close")?.addEventListener("click", () => authModal.close());
authModal?.addEventListener("click", (event) => {
  if (event.target === authModal) {
    authModal.close();
  }
});

urlForm?.addEventListener("submit", (event) => {
  if (document.body.dataset.authenticated !== "true") {
    event.preventDefault();
    openAuthModal("login");
  }
});

if (document.body.dataset.authModal) {
  openAuthModal(document.body.dataset.authModal);
}

if (document.body.dataset.proModal === "true" && proModal && !proModal.open) {
  proModal.showModal();
}

function closeProModal() {
  if (proModal?.open) {
    proModal.close();
  }
}

proModal?.querySelector(".modal-close")?.addEventListener("click", closeProModal);
proModal?.querySelector("[data-pro-decline]")?.addEventListener("click", closeProModal);
proModal?.addEventListener("click", (event) => {
  if (event.target === proModal) {
    closeProModal();
  }
});

function finishSignupOptIn(marketingOptIn) {
  if (!pendingSignupForm) {
    return;
  }

  const form = pendingSignupForm;
  const checkbox = form.querySelector('input[name="marketing_opt_in"]');
  checkbox.checked = marketingOptIn;
  form.dataset.optInPromptHandled = "true";
  pendingSignupForm = null;
  emailOptInModal.close();
  form.requestSubmit();
}

signupForms.forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (form.dataset.optInPromptHandled === "true") {
      delete form.dataset.optInPromptHandled;
      return;
    }

    const checkbox = form.querySelector('input[name="marketing_opt_in"]');
    if (!checkbox || checkbox.checked || !emailOptInModal) {
      return;
    }

    event.preventDefault();
    pendingSignupForm = form;
    emailOptInModal.showModal();
  });
});

emailOptInModal?.querySelector("[data-email-opt-in-accept]")?.addEventListener("click", () => {
  finishSignupOptIn(true);
});

emailOptInModal?.querySelector("[data-email-opt-in-decline]")?.addEventListener("click", () => {
  finishSignupOptIn(false);
});

emailOptInModal?.addEventListener("close", () => {
  pendingSignupForm = null;
});

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
    .some((cookie) => cookie === `mytubenow_download=${token}`);
}

function clearDownloadToken() {
  document.cookie = "mytubenow_download=; Max-Age=0; path=/; SameSite=Lax";
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
