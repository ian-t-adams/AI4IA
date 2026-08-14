export function isCurrentSessionGeneration(
  capturedSession: string | null,
  capturedGeneration: number,
  currentSession: string | null,
  currentGeneration: number,
): boolean {
  return (
    capturedSession === currentSession &&
    capturedGeneration === currentGeneration
  );
}
