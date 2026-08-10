"use client";

export function focusMainContent() {
  const main = document.getElementById("main");
  if (!main) return;
  if (!main.hasAttribute("tabindex")) main.setAttribute("tabindex", "-1");
  main.focus();
}

export function SkipLink() {
  return (
    <a
      href="#main"
      className="skip-link"
      onClick={focusMainContent}
    >
      Skip to main content
    </a>
  );
}
