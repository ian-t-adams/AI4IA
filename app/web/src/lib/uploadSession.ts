import type { LibraryDocument } from "./library";
import type {
  AttachmentCapabilities,
  DocumentSummary,
  Session,
} from "./types";

export type BoundUploadResult =
  | { path: "library"; document: LibraryDocument; session: Session }
  | { path: "session"; document: DocumentSummary };

export async function performBoundUpload({
  capabilities,
  sessionId,
  file,
  isCurrent,
  uploadLibrary,
  associateLibrary,
  uploadSession,
  onAssociating,
}: {
  capabilities: AttachmentCapabilities;
  sessionId: string;
  file: File;
  isCurrent: () => boolean;
  uploadLibrary: (file: File) => Promise<LibraryDocument>;
  associateLibrary: (sessionId: string, documentId: string) => Promise<Session>;
  uploadSession: (sessionId: string, file: File) => Promise<DocumentSummary>;
  onAssociating: () => void;
}): Promise<BoundUploadResult | null> {
  if (capabilities.ingestPath === "library") {
    const document = await uploadLibrary(file);
    if (!isCurrent()) return null;
    onAssociating();
    const session = await associateLibrary(sessionId, document.id);
    if (!isCurrent()) return null;
    return { path: "library", document, session };
  }
  const document = await uploadSession(sessionId, file);
  if (!isCurrent()) return null;
  return { path: "session", document };
}
