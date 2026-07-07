// AI4IA documentation index. Hand-maintained map of the repo's Markdown docs so the
// static docs hub links out to the authoritative source (rendered on GitHub).
window.AI4IA_DOCS = {
  repoBase: "https://github.com/ian-t-adams/AI4IA/blob/main/",
  sections: [
    {
      group: "Start here",
      docs: [
        { path: "README.md", title: "Repository overview", desc: "What AI4IA is, current state, layout, key decisions and how to deploy." },
        { path: "docs/user-guide.md", title: "User guide", desc: "How to use the app: chat, agents, voice, library, media and admin." },
        { path: "docs/architecture.md", title: "Architecture", desc: "Governed multi-model design, request lifecycle, components and MCP planes." },
      ],
    },
    {
      group: "Reference",
      docs: [
        { path: "docs/configuration-reference.md", title: "Configuration reference", desc: "Every feature/env/parameter mapping across bicep, params, workflow vars and app env." },
        { path: "docs/region-capability-matrix.md", title: "Region & capability map", desc: "Which models and capabilities live in East US 2, Sweden Central and West US." },
        { path: "docs/naming-and-tagging.md", title: "Naming & tagging", desc: "Resource naming tokens and the tag scheme applied across the deployment." },
        { path: "docs/foundry-toolbox.md", title: "Foundry toolbox", desc: "The official MCP plane: toolbox, skills, A2A and the private tool catalog." },
        { path: "docs/document-multimodal-understanding.md", title: "Document & multimodal understanding", desc: "Content Understanding ingest, retrieval, compute and the library." },
        { path: "infra/modules/README.md", title: "Bicep modules", desc: "Conventions for the composable infra modules." },
      ],
    },
    {
      group: "Runbooks",
      docs: [
        { path: "docs/runbooks/deployment.md", title: "Deployment runbook", desc: "One-time OIDC setup, custom domains, and the continuous deploy workflow." },
        { path: "docs/runbooks/feature-enablement.md", title: "Feature enablement", desc: "The authoritative flag list and how to safely turn features on/off." },
        { path: "docs/runbooks/teardown.md", title: "Teardown & rebuild", desc: "Inventory, teardown and soft-delete purge procedures." },
      ],
    },
    {
      group: "Component docs",
      docs: [
        { path: "app/api/README.md", title: "API (FastAPI) README", desc: "Backend responsibilities, feature gates and the container image contract." },
        { path: "app/web/README.md", title: "Web (Next.js) README", desc: "Frontend surfaces, runtime feature visibility and the same-origin API proxy." },
        { path: "proxy/README.md", title: "Model proxy README", desc: "The vendored SimpleL7Proxy gateway and the AI4IA Dockerfile." },
      ],
    },
    {
      group: "Audits",
      docs: [
        { path: "docs/brutal-audit.md", title: "Brutal repo audit", desc: "An unsentimental review of reliability, security and cost, with the fixes shipped." },
        { path: "docs/audit-2026-06-findings.md", title: "June 2026 audit findings", desc: "Detailed findings log from the June 2026 review pass." },
      ],
    },
  ],
};
