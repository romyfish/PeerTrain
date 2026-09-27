(() => {
  "use strict";

  const drawer = document.getElementById("promptEditor");
  if (!drawer || typeof bootstrap === "undefined") {
    return;
  }

  const editor = bootstrap.Offcanvas.getOrCreateInstance(drawer);
  const returnSelector = drawer.dataset.returnFocus;
  const typeField = drawer.querySelector("[name='type']");
  const contractCopy = drawer.querySelector("#protected-contract-copy");
  const contractData = document.getElementById("prompt-protected-contracts");
  let contracts = {};
  if (contractData) {
    try {
      contracts = JSON.parse(contractData.textContent);
    } catch (_error) {
      contracts = {};
    }
  }

  typeField?.addEventListener("change", () => {
    if (contractCopy) {
      contractCopy.textContent =
        contracts[typeField.value] ||
        "Select a prompt type to view its protected runtime contract.";
    }
  });

  drawer.addEventListener("shown.bs.offcanvas", () => {
    const firstError = drawer.querySelector(".invalid-feedback");
    const firstInvalid = firstError?.previousElementSibling?.querySelector(
      "input:not([type='hidden']), select, textarea"
    );
    const firstField = drawer.querySelector(
      "input:not([type='hidden']):not([type='checkbox']), select, textarea"
    );
    (firstInvalid || firstField)?.focus();
  });

  drawer.addEventListener("hidden.bs.offcanvas", () => {
    const url = new URL(window.location.href);
    url.searchParams.delete("create");
    url.searchParams.delete("edit");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);

    const returnTarget = returnSelector
      ? document.querySelector(returnSelector)
      : document.getElementById("prompt-new-button");
    returnTarget?.focus({ preventScroll: true });
  });

  if (drawer.dataset.autoOpen === "true") {
    editor.show();
  }
})();
