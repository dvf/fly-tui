"""The `/` filter bar: free text plus `key:value` tokens.

    web state:stopped region:fra
    app:acme-* acct:acme

Tokens match the whole field as a case-insensitive glob (`app:acme-*`), free
text matches anywhere in any column. Repeating a key ORs its values
(`region:fra region:ams`); different keys AND together. A leading `-`
negates a token (`-state:started`).

In the multi-account view the filter is global: it narrows the machines table
and prunes the sidebar to the accounts, orgs and apps that match.
"""

import fnmatch
import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Filter key -> Machine attribute. Aliases are accepted for convenience.
KEYS = {
    "app": "app",
    "org": "org",
    "acct": "account",
    "account": "account",
    "state": "state",
    "region": "region",
}


@dataclass
class Filter:
    terms: List[str] = field(default_factory=list)
    include: Dict[str, List[str]] = field(default_factory=dict)
    exclude: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.terms or self.include or self.exclude)

    def matches(self, machine) -> bool:
        for attr, patterns in self.include.items():
            value = str(getattr(machine, attr, "")).lower()
            if not any(_glob(value, p) for p in patterns):
                return False
        for attr, patterns in self.exclude.items():
            value = str(getattr(machine, attr, "")).lower()
            if any(_glob(value, p) for p in patterns):
                return False
        if self.terms:
            haystack = machine.search_text()
            if not all(t in haystack for t in self.terms):
                return False
        return True

    @property
    def targets_machines(self) -> bool:
        """True if any token is about machines (`state:`, `region:`), not names."""
        return any(a in MACHINE_KEYS for a in (*self.include, *self.exclude))

    def matches_scope(self, account: str, org: Optional[str] = None,
                      app: Optional[str] = None) -> bool:
        """Whether the filter matches an account, org or app by name alone.

        Used to keep apps with no matching machines in the sidebar (an app with
        0 machines, or one that failed to load) when the filter names the app
        itself. Filters with `state:` or `region:` tokens are about machines,
        so they never match by name.
        """
        if self.targets_machines:
            return False
        fields = {"account": account, "org": org, "app": app}
        for attr, patterns in self.include.items():
            value = fields.get(attr)
            if value is None or not any(_glob(value.lower(), p) for p in patterns):
                return False
        for attr, patterns in self.exclude.items():
            value = fields.get(attr)
            if value is not None and any(_glob(value.lower(), p) for p in patterns):
                return False
        if self.terms:
            haystack = " ".join(v for v in (account, org, app) if v).lower()
            if not all(t in haystack for t in self.terms):
                return False
        return True


# Filter keys about machines rather than accounts, orgs or apps.
MACHINE_KEYS = {"state", "region"}


def _glob(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value, pattern)


def _split(text: str) -> List[str]:
    try:
        return shlex.split(text)
    except ValueError:  # unbalanced quote while typing
        return text.split()


def parse_filter(text: str) -> Filter:
    """Parse filter-bar text into a Filter."""
    f = Filter()
    for word in _split(text.strip()):
        negate = word.startswith("-") and ":" in word
        body = word[1:] if negate else word
        key, sep, value = body.partition(":")
        attr = KEYS.get(key.lower()) if sep else None
        if attr and value:
            target = f.exclude if negate else f.include
            target.setdefault(attr, []).append(value.lower())
        elif attr:
            continue  # "state:" with nothing after it yet
        else:
            f.terms.append(word.lower())
    return f


# `f` quick toggle: all -> started -> stopped -> all
STATE_MODES: Tuple[str, ...] = ("all", "started", "stopped")


def next_state_mode(mode: str) -> str:
    i = STATE_MODES.index(mode) if mode in STATE_MODES else 0
    return STATE_MODES[(i + 1) % len(STATE_MODES)]
