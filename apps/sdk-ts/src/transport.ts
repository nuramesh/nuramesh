/** Per-call options. */
export interface RequestOptions {
  /** Aborts the call (a `network_error` is thrown). */
  signal?: AbortSignal;
  /** Extra headers for this call only. */
  headers?: Record<string, string>;
}

/** The optional header of a mutating call: the same key returns the first answer again. The SDK
 *  offers it on every such operation except halt and pause (the kill switch never needs it). */
export interface IdempotencyHeaders {
  "Idempotency-Key"?: string;
}
