// One real paid call to the live API, from a throwaway wallet that lives only on this computer.
//
//   node scripts/paid-test.mjs           first run: makes the wallet and prints its address
//   (send it about $0.05 of USDC on Base; no ETH needed, the facilitator pays the gas)
//   node scripts/paid-test.mjs           second run: pays $0.001 and prints the answer + receipt
//
// The key is saved in worker/.test-wallet (never committed). It only ever holds a few cents.
import fs from "node:fs";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { wrapFetchWithPayment, x402Client, decodePaymentResponseHeader } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm/exact/client";

const API = process.env.API || "https://api.settlewatch.com";
const TARGET = process.argv[2] || "https://api.bitrefill.com/x402/products";
const FILE = new URL("../.test-wallet", import.meta.url);

if (!fs.existsSync(FILE)) {
  fs.writeFileSync(FILE, generatePrivateKey() + "\n", { mode: 0o600 });
  const a = privateKeyToAccount(fs.readFileSync(FILE, "utf8").trim());
  console.log(`Test wallet created: ${a.address}`);
  console.log("Send it about $0.05 of USDC on Base (from the Base app), then run this again.");
  process.exit(0);
}
const account = privateKeyToAccount(fs.readFileSync(FILE, "utf8").trim());
console.log(`Paying from ${account.address}`);
const client = new x402Client().register("eip155:*", new ExactEvmScheme(account));
const pay = wrapFetchWithPayment(fetch, client);
const res = await pay(`${API}/v1/check?url=${encodeURIComponent(TARGET)}`);
const body = await res.text();
console.log(`HTTP ${res.status}`);
try { console.log(JSON.stringify(JSON.parse(body), null, 2).slice(0, 1500)); } catch { console.log(body.slice(0, 1500)); }
const receipt = res.headers.get("payment-response");
if (receipt) {
  const r = decodePaymentResponseHeader(receipt);
  console.log(`\nSettled: ${r.success}  tx: https://basescan.org/tx/${r.transaction}`);
} else {
  console.log("\nNo payment receipt in the response.");
}
