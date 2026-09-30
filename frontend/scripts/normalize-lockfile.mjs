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
 *   node scripts/normalize-lockfile.mjs --check   # exit 1 unless every package is on npmjs.org
 *
 * --check is stricter than the rewrite, and CI runs it before `npm ci`: that
 * install runs this postinstall, which would clean the file before a later
 * check could read it.
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

if (checkOnly) {
  // Each package must resolve to exactly the tarball npm writes for its own name
  // and version on the public registry, with a sha512 hash. That rejects mirror
  // hosts, git and tarball sources (github.com, codeload, git+ssh:), and an entry
  // quietly pointed at another package's tarball.
  const bad = [];
  let checked = 0;
  for (const [path, pkg] of Object.entries(JSON.parse(original).packages ?? {})) {
    if (!path || pkg.inBundle) continue; // the project itself; bundled deps ship inside their parent's tarball
    checked++;
    const name = pkg.name ?? path.slice(path.lastIndexOf('node_modules/') + 'node_modules/'.length);
    const expected = `${CANONICAL}${name}/-/${name.split('/').pop()}-${pkg.version}.tgz`;
    if (pkg.resolved !== expected) bad.push(`${path}: resolved ${pkg.resolved ?? '(none)'}, expected ${expected}`);
    else if (!pkg.integrity?.startsWith('sha512-')) bad.push(`${path}: no sha512 integrity hash`);
  }
  if (bad.length) {
    console.error(`package-lock.json has ${bad.length} package(s) not locked to ${CANONICAL}:`);
    for (const line of bad) console.error(`  ${line}`);
    console.error('Mirror URLs: run `npm run lockfile:normalize`. Anything else must come from the public registry.');
    process.exit(1);
  }
  console.log(`package-lock.json: all ${checked} packages resolve to ${CANONICAL} with sha512 integrity`);
  process.exit(0);
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

if (normalized === original) process.exit(0);

const rewritten = [...hostsOf(original)].filter(([host]) => host !== CANONICAL && !nonRegistry.has(host));
const summary = rewritten.map(([host, n]) => `${n} from ${host}`).join(', ');

writeFileSync(lockfile, normalized);
console.log(`Normalized package-lock.json to ${CANONICAL} (${summary})`);
