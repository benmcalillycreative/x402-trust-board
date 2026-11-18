export type Verdict = "ok" | "caution" | "avoid" | "unknown";
export interface CheckResult {
  url: string;
  host: string;
  verdict: Verdict;
  reasons: string[];
  endpoint: Record<string, unknown> | null;
  seller: Record<string, unknown> | null;
  pay_to: { address: string; proven_by_seller: string | null; matches_listing: boolean | null; contradicts_seller_proof: boolean } | null;
  board: string;
}
export interface GuardOptions {
  mode?: "paid" | "free";
  onCaution?: "allow" | "block";
  onUnknown?: "allow" | "block";
  onError?: "allow" | "block";
  cacheSeconds?: number;
  onResult?: (result: CheckResult | { url: string; verdict: "error"; reasons: string[] }) => void;
  fetch?: typeof fetch;
  api?: string;
  board?: string;
}
export declare const DEFAULT_API: string;
export declare const DEFAULT_BOARD: string;
export declare class PaymentBlocked extends Error { result: CheckResult; }
export declare function withTrustGuard<T extends { onBeforePaymentCreation: (hook: any) => unknown }>(client: T, opts?: GuardOptions): T;
export declare function checkFree(url: string, payTo?: string, opts?: { board?: string; fetch?: typeof fetch }): Promise<CheckResult>;
