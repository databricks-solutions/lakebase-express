/**
 * Keep package-lock.json portable across networks.
 *
 * npm records whichever registry it fetched from in every "resolved" URL it
 * writes. A lockfile committed from behind a private mirror therefore pins
 * tarball URLs to a host nobody outside that network can resolve, and the
 * lockfile host wins over the configured registry — so `npm install` fails with
 * ENOTFOUND per package until the lockfile and node_modules are deleted.
 *
 * This rewrites the registry host of every "resolved" URL back to the canonical
 * public one. Nothing else is touched: integrity hashes stay as they are (they
 * cover the tarball contents, which are identical whichever mirror serves
 * them), and no version is re-resolved.
 *
 * A canonical lockfile does not force anyone to fetch from npmjs.org. npm's
 * default `replace-registry-host=npmjs` substitutes the registry actually in
 * effect for the lockfile's host, so a private mirror configured in ~/.npmrc is
 * still what gets downloaded from.
 *
 * Runs as `postinstall`, so a dependency added behind a mirror is normalized
 * before it can be committed. Also runnable directly:
 *
 *   node scripts/normalize-lockfile.mjs           # rewrite in place
 *   node scripts/normalize-lockfile.mjs --check   # report only, exit 1 if dirty
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const CANONICAL = 'https://registry.npmjs.org/';
const lockfile = join(dirname(dirname(fileURLToPath(import.meta.url))), 'package-lock.json');
const checkOnly = process.argv.includes('--check');

let original;
try {
  original = readFileSync(lockfile, 'utf8');
} catch (err) {
  if (err.code === 'ENOENT') process.exit(0); // no lockfile yet (e.g. --no-package-lock)
  throw err;
}

// Only the registry host is replaced, and only in "resolved" values. Tarballs
// resolved from elsewhere (git, https tarball deps) are left alone: rewriting
// those would point at packages that do not exist on the registry.
const nonRegistry = new Set();
const normalized = original.replace(
  /("resolved":\s*")(https?:\/\/[^/"]+\/)/g,
  (match, prefix, host) => {
    if (host === CANONICAL) return match;
    // A registry mirror serves the same /<name>/-/<file>.tgz layout npm expects.
    // Anything else (github.com, codeload, a gist) is a real source, not a mirror.
    if (/^https?:\/\/(github\.com|codeload\.github\.com|gitlab\.com|bitbucket\.org)\//.test(host)) {
      nonRegistry.add(host);
      return match;
    }
    return prefix + CANONICAL;
  },
);

const hostsOf = (text) => {
  const found = new Map();
  for (const [, host] of text.matchAll(/"resolved":\s*"(https?:\/\/[^/"]+\/)/g)) {
    found.set(host, (found.get(host) ?? 0) + 1);
  }
  return found;
};

if (normalized === original) {
  if (!checkOnly) process.exit(0);
  console.log(`package-lock.json is canonical (${[...hostsOf(original).keys()].join(', ') || 'no resolved URLs'})`);
  process.exit(0);
}

const rewritten = [...hostsOf(original)].filter(([host]) => host !== CANONICAL && !nonRegistry.has(host));
const summary = rewritten.map(([host, n]) => `${n} from ${host}`).join(', ');

if (checkOnly) {
  console.error(`package-lock.json pins a non-canonical registry: ${summary}`);
  console.error('Run `npm run lockfile:normalize` before committing.');
  process.exit(1);
}

writeFileSync(lockfile, normalized);
console.log(`Normalized package-lock.json to ${CANONICAL} (${summary})`);
