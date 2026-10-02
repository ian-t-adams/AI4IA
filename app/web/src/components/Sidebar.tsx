"use client";

import { useId, useMemo, useState } from "react";
import type { RefObject } from "react";
import type { Session } from "@/lib/types";
import type { WorkspaceView } from "@/lib/workspaceView";
import { DOCS_INDEX_URL, STATUS_URL } from "@/lib/docs";
import { groupConversations, matchesConversationQuery } from "@/lib/conversationGroups";
import { AdminLink } from "./AdminLink";
import { EditableSessionTitle } from "./EditableSessionTitle";
import { Icon, type IconName } from "./Icon";
import { UserMenu } from "./UserMenu";
import { useModalFocus, useModalKeyDown } from "./useModalFocus";
import { WorkflowApprovalInboxEntry } from "./WorkflowApprovalInbox";

// The left navigation owns conversations and true destinations. Expanded, it
// shows New chat, the destinations, searchable conversations and one compact
// utility/account area; collapsed, it is an icon rail; as a drawer (narrow
// screens) it is a labelled modal dialog that traps focus and returns it.
export type SidebarMode = "expanded" | "collapsed" | "drawer";

interface Destination {
  view: WorkspaceView;
  label: string;
  icon: IconName;
  // Destinations that can move the conversation (running a workflow, editing
  // an image into the active chat) follow the same navigation lock as
  // switching conversations.
  lockable: boolean;
}

// Show the search field once the list is long enough to need one.
const SEARCH_THRESHOLD = 6;

export function Sidebar({
  mode = "expanded",
  sessions,
  activeId,
  onSelect,
  onNewChat,
  onDelete,
  onRename,
  deletingIds,
  view = "chat",
  onNavigate,
  libraryAvailable = false,
  photoAvatarsAvailable = false,
  onOpenDeletionStatus,
  onBeforeSignOut,
  onCollapse,
  onExpand,
  expandLabel = "Expand sidebar",
  openerRef,
  disabled = false,
  disabledReason,
}: {
  mode?: SidebarMode;
  sessions: Session[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
  deletingIds?: ReadonlySet<string>;
  view?: WorkspaceView;
  onNavigate?: (view: WorkspaceView) => void;
  /** Present only while the deployment offers the document library. */
  libraryAvailable?: boolean;
  /** Present only while the server reports photo avatars enabled. */
  photoAvatarsAvailable?: boolean;
  onOpenDeletionStatus: () => void;
  onBeforeSignOut?: () => boolean | void;
  onCollapse?: () => void;
  onExpand?: () => void;
  expandLabel?: string;
  openerRef?: RefObject<HTMLElement | null>;
  disabled?: boolean;
  /** Shown while navigation is locked, explaining why and how to recover. */
  disabledReason?: string;
}) {
  const drawer = mode === "drawer";
  const drawerFocusRef = useModalFocus<HTMLElement>(drawer, openerRef);
  const onDrawerKeyDown = useModalKeyDown<HTMLElement>(onCollapse ?? (() => {}), drawer);
  // Every soft-disabled control points at the visible lock reason, so screen
  // reader users hear the same recovery guidance sighted users read.
  const lockHintId = useId();
  const headingId = useId();
  const describedBy = disabled && disabledReason ? lockHintId : undefined;
  const [query, setQuery] = useState("");

  const destinations: Destination[] = [
    ...(libraryAvailable
      ? [{ view: "library" as const, label: "Document library", icon: "library" as const, lockable: true }]
      : []),
    ...(photoAvatarsAvailable
      ? [{ view: "avatars" as const, label: "Photo avatars", icon: "avatar" as const, lockable: false }]
      : []),
    { view: "studio", label: "Agents & workflows", icon: "studio", lockable: true },
  ];

  const visible = useMemo(
    () => sessions.filter((session) => matchesConversationQuery(session.title, query)),
    [query, sessions],
  );
  const groups = useMemo(() => groupConversations(visible), [visible]);

  const go = (destination: Destination) => {
    if (disabled && destination.lockable) return;
    onNavigate?.(destination.view);
  };

  const newChat = () => {
    if (disabled) return;
    onNewChat();
  };

  if (mode === "collapsed") {
    return (
      <nav
        className="app-sidebar"
        data-mode="collapsed"
        aria-label="Chat sessions"
        style={{ overflow: "hidden" }}
      >
        <div className="sidebar-head">
          {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark */}
          <img src="/ai4ia-mark.png" alt="" aria-hidden="true" width={28} height={28} style={{ borderRadius: 6 }} />
          <button
            ref={(element) => {
              if (element && openerRef) openerRef.current = element;
            }}
            type="button"
            className="icon-btn"
            onClick={onExpand}
            aria-label={expandLabel}
            title={expandLabel}
          >
            <Icon name="panel-left" />
          </button>
        </div>
        <div className="sidebar-rail">
          {disabled && disabledReason ? (
            <p id={lockHintId} role="status" className="visually-hidden">
              {disabledReason}
            </p>
          ) : null}
          <ul className="sidebar-rail-group" aria-label="Destinations">
            <li>
              <button
                type="button"
                className="icon-btn rail-new-chat"
                onClick={newChat}
                aria-disabled={disabled || undefined}
                aria-describedby={describedBy}
                aria-label="New chat"
                title="New chat"
              >
                <Icon name="plus" />
              </button>
            </li>
            <li>
              <button
                type="button"
                className="icon-btn"
                onClick={() => onNavigate?.("chat")}
                aria-current={view === "chat" ? "page" : undefined}
                aria-label="Conversation"
                title="Conversation"
              >
                <Icon name="chat" />
              </button>
            </li>
            {destinations.map((destination) => (
              <li key={destination.view}>
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => go(destination)}
                  aria-current={view === destination.view ? "page" : undefined}
                  aria-disabled={(disabled && destination.lockable) || undefined}
                  aria-describedby={destination.lockable ? describedBy : undefined}
                  aria-label={destination.label}
                  title={destination.label}
                >
                  <Icon name={destination.icon} />
                </button>
              </li>
            ))}
          </ul>
          <div className="sidebar-rail-spacer" />
          <ul className="sidebar-rail-group" aria-label="Utilities and account">
            <li>
              <WorkflowApprovalInboxEntry disabled={disabled} compact />
            </li>
            <li>
              <button
                type="button"
                className="icon-btn"
                onClick={() => onNavigate?.("settings")}
                aria-current={view === "settings" ? "page" : undefined}
                aria-label="Settings"
                title="Settings"
              >
                <Icon name="settings" />
              </button>
            </li>
            <li className="rail-optional">
              <a
                className="icon-btn"
                href={DOCS_INDEX_URL}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Documentation (opens in new tab)"
                title="Documentation"
              >
                <Icon name="docs" />
              </a>
            </li>
            <li className="rail-optional">
              <a
                className="icon-btn"
                href={STATUS_URL}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Status (opens in new tab)"
                title="Deployment status"
              >
                <Icon name="status" />
              </a>
            </li>
            <li>
              <AdminLink disabled={disabled} disabledReasonId={describedBy} compact />
            </li>
          </ul>
        </div>
      </nav>
    );
  }

  return (
    <nav
      ref={drawerFocusRef}
      onKeyDown={onDrawerKeyDown}
      className="app-sidebar"
      data-mode={mode}
      role={drawer ? "dialog" : "navigation"}
      aria-modal={drawer ? true : undefined}
      aria-label="Chat sessions"
      style={{ overflow: "hidden" }}
    >
      <div className="sidebar-head">
        <span className="sidebar-brand">
          {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark */}
          <img src="/ai4ia-mark.png" alt="" aria-hidden="true" width={28} height={28} />
          AI4IA
        </span>
        {onCollapse ? (
          <button
            type="button"
            className="icon-btn"
            onClick={onCollapse}
            aria-label="Collapse sidebar"
            title={drawer ? "Close" : "Collapse sidebar"}
          >
            <Icon name={drawer ? "close" : "panel-left"} />
          </button>
        ) : null}
      </div>
      <div
        className="sidebar-scroll"
        data-testid="sidebar-scroll"
        style={{ minHeight: 0, overflowY: "auto", overflowX: "hidden" }}
      >
        {disabled && disabledReason ? (
          <p id={lockHintId} role="status" className="sidebar-lock">
            {disabledReason}
          </p>
        ) : null}
        <button
          type="button"
          className="btn btn-primary sidebar-new-chat"
          onClick={newChat}
          aria-disabled={disabled || undefined}
          aria-describedby={describedBy}
        >
          <Icon name="plus" size={18} />
          New chat
        </button>
        <ul className="sidebar-nav" aria-label="Destinations">
          {destinations.map((destination) => (
            <li key={destination.view}>
              <button
                type="button"
                className="sidebar-link"
                onClick={() => go(destination)}
                aria-current={view === destination.view ? "page" : undefined}
                aria-disabled={(disabled && destination.lockable) || undefined}
                aria-describedby={destination.lockable ? describedBy : undefined}
              >
                <Icon name={destination.icon} />
                <span>{destination.label}</span>
              </button>
            </li>
          ))}
        </ul>
        <section className="sidebar-conversations" aria-labelledby={headingId}>
          <h2 className="sidebar-section-title" id={headingId}>
            Conversations
          </h2>
          {sessions.length >= SEARCH_THRESHOLD || query ? (
            <div className="sidebar-search">
              <Icon name="search" size={16} />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search conversations"
                aria-label="Search conversations"
              />
            </div>
          ) : null}
          {sessions.length === 0 ? (
            <p className="conversation-empty">No conversations yet.</p>
          ) : groups.length === 0 ? (
            <p className="conversation-empty" role="status">
              No conversations match “{query.trim()}”.
            </p>
          ) : (
            groups.map((group) => (
              <div key={group.title ?? "all"}>
                {group.title ? (
                  <h3 className="conversation-group-title">{group.title}</h3>
                ) : null}
                <ul className="conversation-group">
                  {group.items.map((session) => {
                    const current = session.id === activeId && view === "chat";
                    const deleting = deletingIds?.has(session.id) ?? false;
                    return (
                      <li
                        key={session.id}
                        className="conversation-row"
                        data-current={current || undefined}
                      >
                        <EditableSessionTitle
                          title={session.title || "Untitled"}
                          onSave={(title) => onRename(session.id, title)}
                          onOpen={() => onSelect(session.id)}
                          current={session.id === activeId}
                          disabled={disabled}
                          disabledReasonId={lockHintId}
                          compact
                        />
                        <button
                          type="button"
                          className="conversation-delete"
                          onClick={() => {
                            if (disabled || deleting) return;
                            onDelete(session.id);
                          }}
                          disabled={deleting}
                          aria-disabled={disabled || deleting || undefined}
                          aria-busy={deleting || undefined}
                          aria-label={`Delete ${session.title || "conversation"}`}
                          aria-describedby={describedBy}
                          title={disabled ? undefined : "Delete"}
                        >
                          <Icon name="trash" size={16} />
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))
          )}
          <button type="button" className="sidebar-quiet-action" onClick={onOpenDeletionStatus}>
            Deletion status
          </button>
        </section>
      </div>
      <div className="sidebar-footer" aria-label="Utilities and account" role="group">
        <WorkflowApprovalInboxEntry disabled={disabled} />
        <button
          type="button"
          className="sidebar-link"
          onClick={() => onNavigate?.("settings")}
          aria-current={view === "settings" ? "page" : undefined}
        >
          <Icon name="settings" />
          <span>Settings</span>
        </button>
        <div className="sidebar-footer-row">
          <a
            className="sidebar-link"
            href={DOCS_INDEX_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Documentation (opens in new tab)"
            title="Browse the AI4IA documentation hub"
          >
            <Icon name="docs" />
            <span>Docs</span>
          </a>
          <a
            className="sidebar-link"
            href={STATUS_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Status (opens in new tab)"
            title="Live deployment health and service status"
          >
            <Icon name="status" />
            <span>Status</span>
          </a>
        </div>
        <AdminLink disabled={disabled} disabledReasonId={describedBy} />
        <UserMenu onBeforeSignOut={onBeforeSignOut} />
      </div>
    </nav>
  );
}
