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

export async function commitLatestSessionMutation<T>({
  capturedSession,
  capturedGeneration,
  currentSession,
  currentGeneration,
  operation,
  commit,
}: {
  capturedSession: string;
  capturedGeneration: number;
  currentSession: () => string | null;
  currentGeneration: () => number;
  operation: () => Promise<T>;
  commit: (value: T) => void;
}): Promise<boolean> {
  const value = await operation();
  if (
    !isCurrentSessionGeneration(
      capturedSession,
      capturedGeneration,
      currentSession(),
      currentGeneration(),
    )
  ) return false;
  commit(value);
  return true;
}
