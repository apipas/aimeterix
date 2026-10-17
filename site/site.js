// Copy buttons on every command block, and the phone menu. No tracking, no cookies.
document.querySelectorAll(".term").forEach((term) => {
  const pre = term.querySelector("pre");
  const button = document.createElement("button");
  button.className = "copy";
  button.type = "button";
  button.textContent = "Copy";
  button.addEventListener("click", async () => {
    const text = [...pre.childNodes]
      .filter((n) => !(n.classList && (n.classList.contains("p") || n.classList.contains("c"))))
      .map((n) => n.textContent).join("").trim();
    try { await navigator.clipboard.writeText(text); } catch { return; }
    button.textContent = "Copied"; button.classList.add("done");
    setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1500);
  });
  term.appendChild(button);
});
const menuButton = document.querySelector(".menu-btn");
if (menuButton) menuButton.addEventListener("click", () => document.querySelector("nav.menu").classList.toggle("open"));
