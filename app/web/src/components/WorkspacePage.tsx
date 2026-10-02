"use client";

import type { ReactNode, RefObject } from "react";

import { Icon } from "./Icon";

// A destination page inside the shell: one heading (which receives focus when
// the page opens, so screen-reader and keyboard users land on it), an optional
// description and actions, then the page body. On phones the navigation
// drawer opens from here, as it does from the conversation header.
export function WorkspacePage({
  id,
  title,
  description,
  actions,
  onOpenSidebar,
  sidebarButtonRef,
  headingRef,
  children,
}: {
  id: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  onOpenSidebar?: () => void;
  sidebarButtonRef?: RefObject<HTMLElement | null>;
  headingRef?: RefObject<HTMLHeadingElement | null>;
  children: ReactNode;
}) {
  return (
    <section className="page" aria-labelledby={id}>
      <div className="page-inner">
        <header className="page-header">
          {onOpenSidebar ? (
            <button
              ref={(element) => {
                if (element && sidebarButtonRef) sidebarButtonRef.current = element;
              }}
              type="button"
              className="icon-btn"
              onClick={onOpenSidebar}
              aria-label="Open conversation sidebar"
              title="Conversations and destinations"
            >
              <Icon name="menu" />
            </button>
          ) : null}
          <div className="page-heading">
            <h1 id={id} ref={headingRef} tabIndex={-1}>
              {title}
            </h1>
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="page-actions">{actions}</div> : null}
        </header>
        <div className="page-body">{children}</div>
      </div>
    </section>
  );
}
