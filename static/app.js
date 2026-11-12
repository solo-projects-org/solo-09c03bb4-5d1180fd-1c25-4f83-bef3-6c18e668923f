// Progressive enhancement only; the server enforces the same rules.
document.addEventListener("DOMContentLoaded", function () {
  var swapForm = document.querySelector(".swap-pick-form");
  if (swapForm) {
    swapForm.addEventListener("submit", function (event) {
      var picked = swapForm.querySelectorAll("input[name=card_id]:checked");
      if (picked.length !== 2) {
        event.preventDefault();
        window.alert("请勾选正好两张卡片。");
      }
    });
  }
});
