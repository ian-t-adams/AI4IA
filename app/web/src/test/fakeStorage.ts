export function fakeBrowserStorage(
  initial: Record<string, string> = {},
): Storage {
  const data = new Map(Object.entries(initial));
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key) => data.get(key) ?? null,
    key: (index) => [...data.keys()][index] ?? null,
    removeItem: (key) => {
      data.delete(key);
    },
    setItem: (key, value) => {
      data.set(key, String(value));
    },
  };
}

export function installFakeLocalStorage(
  storage: Storage = fakeBrowserStorage(),
): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
  return () => {
    if (descriptor) {
      Object.defineProperty(window, "localStorage", descriptor);
    } else {
      Reflect.deleteProperty(window, "localStorage");
    }
  };
}
