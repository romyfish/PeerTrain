(() => {
  document.querySelectorAll("[data-peertrain-success-toast]").forEach((element) => {
    if (!window.bootstrap?.Toast) return;
    const toast = window.bootstrap.Toast.getOrCreateInstance(element, {
      autohide: true,
      delay: 4500,
    });
    toast.show();
  });
})();
