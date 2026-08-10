"use client";

export function SkipLink() {
  return (
    <a
      href="#main"
      className="skip-link"
      onClick={() => {
        const main = document.getElementById("main");
        if (!main) return;
        if (!main.hasAttribute("tabindex")) main.setAttribute("tabindex", "-1");
        main.focus();
      }}
    >
      Skip to main content
    </a>
  );
}
