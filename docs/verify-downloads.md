# Verify what you downloaded

Every NuraMesh release is signed, and every file and image in it can be checked on your own machine before you
run it. This page shows the checks, what each one proves and what none of them proves.

> **Nothing is published yet.** The commands below apply from the first public release. Until then there is
> nothing to download and no signature to check; the release bundle you may receive from the team is checked
> the same way, with the key or identity the team gives you in a separate message.

You need `cosign` (the Sigstore command line), `openssl` and either `sha256sum` or `shasum`. The checks run on
your machine and change nothing.

## The identity you check against

A release is signed without a stored key: the signature names the workflow run that built it. You decide what
you accept by typing the identity yourself. It is built from the version, so a signature from an older release
does not pass for a newer one:

```sh
VERSION=0.1.0
IDENTITY="https://github.com/nuramesh/nuramesh/.github/workflows/publish.yml@refs/tags/v${VERSION}"
ISSUER="https://token.actions.githubusercontent.com"
```

Never take the identity, a key or a certificate from the folder you are checking. Whoever can replace the
files in that folder can replace those too, and the check would then prove nothing.

## 1. The release files

Download all files of the release into one empty folder, then run the script from the source repository:

```sh
sh scripts/release/verify.sh ./release --version 0.1.0
```

It checks the signature over `SHA256SUMS` first, then the hash of every file the list names, and prints
`checksums: ok (N files)` and `signature: ok`, and ends with the single line `VERIFY OK` only when every check
passed. Any other ending is a failure: it stops with `VERIFY FAILED` and a reason on the first problem (a missing
folder is one too; only missing or unknown arguments print the usage text):

- a missing `cosign`, `openssl` or hash tool (it never skips a check because a tool is missing);
- a signature file it does not expect, for example a key signature next to the Sigstore bundle;
- a symbolic link anywhere in the folder, listed or not;
- an option that does not belong to the mode (`--pubkey` in the default mode; `--version`, `--identity` or
  `--issuer` in dry-run mode), or `--version` together with `--identity`: give one or the other;
- an `--owner-key` that is not one readable public key, even when there is no owner signature to check;
- a line in `SHA256SUMS` with an unusual name (a path that climbs out of the folder, an absolute path, a
  duplicate, a symbolic link);
- a `SHA256SUMS` whose last line has no newline at the end;
- a listed file that is missing or whose hash differs.

Exit codes: `0` only when every check passed (and the last line is `VERIFY OK`); `1` for any failed check
(`VERIFY FAILED` on standard error); `2` for missing or unknown arguments (the usage text). Without
`--owner-key` or an owner signature the script says nothing about one; with `--owner-key` and no
`SHA256SUMS.owner.sig` it prints `owner signature: none` before `VERIFY OK`, so the absence is never silent.

Files in the folder that `SHA256SUMS` does not list are printed as `note: not covered by SHA256SUMS` and are not
trusted.

The same check by hand:

```sh
cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" SHA256SUMS
sha256sum -c SHA256SUMS        # macOS: shasum -a 256 -c SHA256SUMS
```

## 2. The package from the Python index

`pip` does not verify signatures. Download the file and compare its hash with the line in the `SHA256SUMS` you
just verified:

```sh
pip download "nuramesh==${VERSION}" --no-deps -d from-index
( cd from-index && sha256sum nuramesh-*.whl )
```

## 3. A container image

Images are named by digest, never by a tag a registry could move. Take the digest from the verified
`image-digests.txt`:

```sh
IMAGE="ghcr.io/nuramesh/nuramesh-api@sha256:<digest from image-digests.txt>"
cosign verify "$IMAGE" --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
```

Docker and Compose do not verify signatures when they pull. Pulling by the digest from the signed list is what
protects you, and `cosign verify` is the check you run.

## 4. The software bill of materials

Each release carries a CycloneDX 1.6 SBOM for the Python package (from the lockfile, with the dependency graph
and hashes) and one for each image (every operating-system and Python package found in its layers). The SBOM
files are covered by `SHA256SUMS`, so they are checked in step 1. For an image, the SBOM is also attached to
the image and checked with:

```sh
cosign verify-attestation --type cyclonedx "$IMAGE" \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" >/dev/null
```

The script does not check an image's layer blobs against their digests; only `cosign verify` and the
registry's pull by digest do that.

An SBOM lists what is inside. It has no licence data yet, and an image SBOM does not list a file that no
package database or Python metadata names.

## 5. Optional: the owner's second signature

If the release carries `SHA256SUMS.owner.sig`, it is a second signature made by hand with a key kept apart from
the build. Get the public key from the website or the source repository, not from the release:

```sh
sh scripts/release/verify.sh ./release --version 0.1.0 --owner-key ./owner-public.pem --require owner-key
```

The script refuses a key that sits inside the release folder or cannot be read, and it checks an owner signature
whenever the file is present, so a bad one is never ignored.

## What this does and does not prove

- **It proves** that the files were produced by the named workflow for that version and have not changed since.
  The build provenance is SLSA v1.0, Build Level 2.
- **Who can sign:** whoever can create a version tag in the project's repository. That is a small, named
  set of people, and it is the real boundary; the signature does not widen it.
- **It does not prove** that the code or a dependency is free of vulnerabilities or of a malicious change that
  was reviewed and merged.
- Third-party images that Compose pulls (PostgreSQL, Temporal and others) are pinned by digest but are not
  signed by us.
- Container images are not byte-for-byte reproducible; the Python package is.
- Nobody watches the public signing log for the project's identity; a check on your side is the only check.
- `pip` and Compose enforce nothing by themselves. The steps above are steps you run.
