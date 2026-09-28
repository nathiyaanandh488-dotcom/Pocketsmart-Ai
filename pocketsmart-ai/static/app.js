// Disable the submit button while the AI is working so users don't double-submit.
document.querySelectorAll("form[data-loading]").forEach(function (form) {
  form.addEventListener("submit", function () {
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = true;
      button.textContent = button.dataset.loadingText || "Working...";
    }
  });
});

// Preview the outfit photo before upload.
var fileInput = document.getElementById("outfit_image");
var preview = document.getElementById("outfit_preview");
if (fileInput && preview) {
  fileInput.addEventListener("change", function () {
    var file = fileInput.files && fileInput.files[0];
    if (!file) { preview.style.display = "none"; return; }
    preview.src = URL.createObjectURL(file);
    preview.style.display = "block";
  });
}
