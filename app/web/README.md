# app/web — AI4IA Web (Next.js + TypeScript)

Chat UI mirroring the Foundry chat experience plus AI4IA extras.

## Responsibilities (phased)
- **Phase 3:** chat surface with streaming, model picker, parameter controls, system-prompt
  editor, document upload; theming (colors/backgrounds/sizing) and high-contrast a11y modes.
- **Phase 4:** `@`-commands (address agents) and `/`-commands (actions) in the composer.
- **Phase 7:** voice (talk to it), custom imagery/backgrounds via gpt-image-2 / MAI-Image-2.
- **Phase 8:** multi-agent linking + workflow builder UI.

## Stack
- Next.js (App Router) + TypeScript, MSAL for auth, server actions / route handlers calling
  the FastAPI backend. Containerized (`Dockerfile`) and deployed to Azure Container Apps.

## Local dev (to be added in Phase 3)
```
npm install
npm run dev
```

> Scaffolding lands in Phase 3. The model picker is driven by the catalog the API exposes
> from `infra/models.json` (never hard-coded).
