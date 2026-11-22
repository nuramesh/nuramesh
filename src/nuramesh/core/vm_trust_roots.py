"""Verified Merge trust roots: the generic constant `OWNER_ONLY_PATHS`
(`spec/verified-merge/tiers.md` TL-26, TL-28; README M-9).

The generic minimum: paths that are trust roots in **every** repository that uses Verified Merge
(the forge's control files, the three Verified Merge files, the CODEOWNERS places, the git
control files). A changed path matched by a line is tier 3 with the reason `trust_root` and needs
the signed approval of a `trust_root_admin` who is not the author, whatever the tier list or the
policy says (TL-2, GT-12). It names no path of our own source tree: a repository's own
gate-relevant paths are the `extra_trust_roots` of its `.nuramesh/policy.json`, read at the base
tip, which only add (TL-29).

**This tuple is equal to the fenced block `trust-roots-v1` of the spec, line by line, in the same
(byte) order.** The M-9 test parses the block from the spec text and compares; the block and this
constant change together in one PR, with the owner's explicit approval, or not at all (TL-28).

Each line is a TL-11 pattern anchored at the repository root (`*` never crosses `/`, `**` is a
whole segment), never a prefix. The matcher and the case-fold alias rule are
`vm_tiers.is_trust_root`; this file holds the data only and imports nothing: a plain tuple of
`str`, never a list, a set or a computed value.
"""

from __future__ import annotations

OWNER_ONLY_PATHS: tuple[str, ...] = (
    "**/.gitattributes",
    ".gitattributes",
    ".github",
    ".github/**",
    ".gitmodules",
    ".nuramesh",
    ".nuramesh/**",
    ".nuramesh/policy.json",
    ".nuramesh/tiers.json",
    ".nuramesh/trust.json",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)
