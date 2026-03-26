const defaultState = {
  loading: false,
  error: "",
  recommendations: [],
  positions: [],
  history: [],
  analytics: null,
};

export function createPortfolioStore() {
  const state = { ...defaultState };
  const listeners = new Set();

  function notify() {
    listeners.forEach((fn) => fn(state));
  }

  return {
    getState() {
      return state;
    },
    set(patch) {
      Object.assign(state, patch);
      notify();
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}
