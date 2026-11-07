/** The browser side of viewing a secret's values (backend moks/secrets/seal.py).
 *  The private key is made here, cannot be exported, and lives only as long as the dialog: the executor encrypts
 *  the values to its public half, so MOKS's database and API only ever hold ciphertext. */

export interface Sealed { alg: string; epk: JsonWebKey; iv: string; ct: string }
export interface Revealed { side: string; type: string; version: string | null; keys: Record<string, { text?: string; base64?: string }> }

const INFO = new TextEncoder().encode("moks-secret-reveal-v1");

function unb64(s: string): Uint8Array {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}

function uuidBytes(id: string): Uint8Array {
  const hex = id.replace(/-/g, "");
  return Uint8Array.from(hex.match(/../g)!.map((h) => parseInt(h, 16)));
}

export async function newKeyPair(): Promise<{ privateKey: CryptoKey; publicJwk: JsonWebKey }> {
  const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
  const jwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
  return { privateKey: pair.privateKey, publicJwk: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y } };
}

export async function openSealed(privateKey: CryptoKey, sealed: Sealed, jobId: string): Promise<Revealed> {
  const peer = await crypto.subtle.importKey("jwk", sealed.epk, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: peer }, privateKey, 256);
  const hkdf = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: uuidBytes(jobId) as BufferSource, info: INFO }, hkdf, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  const plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(sealed.iv) as BufferSource, additionalData: new TextEncoder().encode(jobId) }, key, unb64(sealed.ct) as BufferSource);
  return JSON.parse(new TextDecoder().decode(plain));
}
