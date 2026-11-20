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
  /** Present when the seller's DNS payout proof was read live: none | match | mismatch | error. */
  dns_live?: "none" | "match" | "mismatch" | "error";
  /** The live DNS check in the spec's terms (payout-proof spec, section 6.2). */
  dns_outcome?: "verified" | "mismatch" | "malformed" | "absent" | "unchecked" | null;
  /** Signature status of the DNS proof that listed this address (only with dns_outcome "verified"). */
  dns_signature?: "valid" | "invalid" | "unverifiable" | "none";
  /** The proof's Issued date (reported, never used to soften a mismatch). */
  dns_issued?: string;
}
export interface GuardOptions {
  mode?: "paid" | "free";
  onCaution?: "allow" | "block";
  onUnknown?: "allow" | "block";
  onError?: "allow" | "block";
  cacheSeconds?: number;
  /** Read the seller's _x402-payto DNS proof live (cached by its TTL, at most 24h). Default true. */
  dnsProofs?: boolean;
  /** When the seller's DNS proof doesn't list the quoted address. Default "block". */
  onProofMismatch?: "block" | "allow";
  /** DNS-over-HTTPS JSON endpoint. Default Cloudflare. */
  doh?: string;
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
