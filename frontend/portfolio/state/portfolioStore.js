/**
 * portfolioStore.js — Reactive state store for the portfolio module.
 *
 * Implements a minimal observer pattern similar to Svelte stores.
 * State is a single mutable object. Calling set() merges a patch into
 * the state and synchronously notifies all subscribers.
 *
 * Usage:
 *   const store = createPortfolioStore()
 *   const unsub = store.subscribe(state => renderUI(state))
 *   store.set({ loading: true })   // triggers all subscribers
 *   unsub()                        // remove listener when done
 */

/** Default state shape — all fields that consumers can read. */
const defaultState = {
  loading: false,        // true while any API request is in-flight
  error: "",             // error message string (empty = no error)
  recommendations: [],   // array of recommendation rows from Supabase
  positions: [],         // array of active position rows (with live current_price)
  history: [],           // array of portfolio_events rows (audit log)
  analytics: null,       // analytics KPI object or null before first fetch
};

/**
 * Create a new portfolio store instance.
 * Each call returns an independent store — typically one per page mount.
 *
 * @returns {{ getState, set, subscribe }} Store interface.
 */
export function createPortfolioStore() {
  // Spread to get a fresh copy of defaults (avoid mutation across instances)
  const state = { ...defaultState };
  const listeners = new Set();

  /** Call all registered subscriber functions with the current state. */
  function notify() {
    listeners.forEach((fn) => fn(state));
  }

  return {
    /** Return the current state object (by reference — do not mutate directly). */
    getState() {
      return state;
    },

    /**
     * Merge patch into state and notify all subscribers.
     * @param {Partial<typeof defaultState>} patch - Fields to update.
     */
    set(patch) {
      Object.assign(state, patch);
      notify();
    },

    /**
     * Register a subscriber function called on every state change.
     * @param {(state: typeof defaultState) => void} fn - Callback.
     * @returns {() => void} Unsubscribe function — call to remove the listener.
     */
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);  // return cleanup function
    },
  };
}
