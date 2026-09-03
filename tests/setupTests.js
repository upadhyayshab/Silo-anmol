import "@testing-library/jest-dom";

// Node's own built-in localStorage (and, in this environment, jsdom's) is
// non-functional without extra CLI configuration neither Vitest nor this repo
// sets up - real browsers don't have this problem, and auth.js's plain
// `localStorage` calls are correct browser code. Rather than depend on either
// runtime's implementation, install a minimal working in-memory stand-in for
// tests, forcing it past any existing non-configurable/getter-only property.
class MemoryStorage {
  constructor() {
    this._store = new Map();
  }
  getItem(key) {
    return this._store.has(key) ? this._store.get(key) : null;
  }
  setItem(key, value) {
    this._store.set(key, String(value));
  }
  removeItem(key) {
    this._store.delete(key);
  }
  clear() {
    this._store.clear();
  }
}

const memoryStorage = new MemoryStorage();
for (const target of [globalThis, window]) {
  Object.defineProperty(target, "localStorage", {
    value: memoryStorage,
    writable: true,
    configurable: true,
  });
}
