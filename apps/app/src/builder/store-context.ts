import { createContext, useContext } from "react";
import { useStore } from "zustand";

import type { BuilderState, BuilderStore } from "./store";

export const BuilderStoreContext = createContext<BuilderStore | null>(null);

/** The builder store from context (one per builder page; tests create their own). */
export function useBuilderStore(): BuilderStore {
  const store = useContext(BuilderStoreContext);
  if (store === null) throw new Error("useBuilderStore must be used inside <BuilderStoreContext>");
  return store;
}

/** Subscribes to one slice of builder state; re-renders only when that slice changes. */
export function useBuilder<T>(selector: (state: BuilderState) => T): T {
  return useStore(useBuilderStore(), selector);
}
