"""Explicit-cue and evidence guards; never execute a meeting action."""
import re


_ARTIFACT = re.compile(
    r"\b(?:documents?|docs?|files?|reports?|spreadsheets?|sheets?|decks?|plans?|"
    r"proposals?|pages?|tickets?|issues?|runbooks?|budgets?|analys(?:is|es)|"
    r"flowcharts?|diagrams?|roadmaps?|comparisons?|forms?|polic(?:y|ies)|"
    r"specifications?|specs?|designs?|presentations?|notes?)\b|\b[A-Z][A-Z0-9]+-\d+\b",
    re.IGNORECASE,
)
_REQUEST = (
    r"(?:please(?:\s+do)?|(?:can|could|would|will)\s+(?:you|someone|somebody|anyone|we)"
    r"(?:\s+from\s+(?:the\s+)?team)?|(?:someone|somebody|anyone)"
    r"(?:\s+from\s+(?:the\s+)?team)?\s+(?:can|could|would|will)|"
    r"let'?s|let\s+us|(?:we|i)\s+need\s+to)"
)
_DIRECT_RETRIEVAL = re.compile(
    r"(?:^|\b" + _REQUEST + r"\s+)(?:find|search|retrieve|locate|open|show|display|"
    r"pull\s+up|bring\s+up|look\s+(?:up|at))\b", re.IGNORECASE)
_DIRECT_TASK = re.compile(
    r"\b(?:please(?:\s+do)?|(?:can|could|would|will)\s+(?:you|someone|somebody|anyone)|"
    r"i(?:'ll|\s+will|\s+can|\s+am\s+going\s+to|'m\s+going\s+to)|"
    r"we(?:'ll|\s+will|\s+should|\s+must|\s+need\s+to)|let\s+me)\s+"
    r"(?:(?:actually|please|possibly|maybe|mind)\s+)?"
    r"(?:take|taking|have\s+a\s+look|look\s+into|invest|fill|complete|finish|arrange|schedule|send|sending|ask|follow|"
    r"investigate|fix|add|create|file|raise|run|prepare|write|update|set|get|"
    r"check|review|confirm|remove|implement|deliver|put|organize|organise)\b", re.IGNORECASE)
_CANCEL = re.compile(
    r"\b(?:do\s+not|don't|no\s+need\s+to|let'?s\s+not)\s+"
    r"(?:create|file|open|raise|add|send|schedule|arrange|do|make|investigate|"
    r"fix|complete|fill|set|get|work|follow|take|update)\b|"
    r"\b(?:scratch\s+that|never\s+mind|no\s+follow.?up\s+needed|"
    r"already\s+(?:done|fixed|completed|resolved|sent))\b", re.IGNORECASE)
_HYPOTHETICAL = re.compile(r"\b(?:if\s+we\s+ever|maybe\s+(?:one\s+day|someday)|just\s+brainstorming)\b", re.IGNORECASE)
_PAST_QUESTION = re.compile(
    r"\b(?:what|which|where|why|how)\b.*\b(?:did|was|were|previous|last|before|decide|agreed)\b",
    re.IGNORECASE,
)
_WORK_TRANSITION = re.compile(
    r"\b(?:should|must|need\s+to|have\s+to|fill|complete|finish|arrange|schedule|"
    r"send|ask|investigate|fix|add|create|run|prepare|write|update|implement|"
    r"deliver|handle|own|owns|taking|leave|set\s+up|pick\s+up)\b|"
    r"\b(?:get|getting)\b.*\b(?:done|ready|set\s+up)\b|"
    r"\b(?:i|we)(?:'ll|\s+will|\s+can)\b", re.IGNORECASE,
)
_ACCEPT = re.compile(r"\b(?:yes|sure|agreed|go\s+ahead|sounds\s+good|next\s+step|let'?s\s+do)\b", re.IGNORECASE)
_GENERIC = frozenset("""
unknown the a an and or to in on at of for from with about this that it those
these we i you they he she one some any else anybody somebody someone please
do did does get go take have has had there here already just last next third
second first job creating create new old views view showing sort which whether
given time right great good talked talk now then need needs going done up out
""".split())


def _speech(text: str) -> str:
    text = re.sub(r"(?m)^[^:\n]{1,40}:\s*", "", text)
    return re.sub(r"[.!?,;:]", " ", text.replace('’', "'"))


def words(text: str) -> set[str]:
    tokens = set(re.findall(r"[\w-]+", text.casefold())) - _GENERIC
    return {token[:-1] if len(token) > 3 and token.endswith('s') else token
            for token in tokens if len(token) >= 3 or any(c.isdigit() for c in token)}


def direct_retrieval_request(fresh: str, recent: str) -> bool:
    return bool(_DIRECT_RETRIEVAL.search(_speech(fresh))
                and _ARTIFACT.search(fresh + ' ' + recent)
                and not re.search(r"\b(?:already\s+found|found\s+it|searched\s+my)\b", fresh, re.IGNORECASE))


def direct_task_request(fresh: str, recent: str = '') -> bool:
    normalized = _speech(fresh)
    return bool(_DIRECT_TASK.search(normalized)
                and not _CANCEL.search(normalized)
                and not _HYPOTHETICAL.search(normalized)
                and not re.search(r"\b(?:put\s+it\s+this\s+way|take\s+it\s+from\s+there|get\s+going)\b", normalized, re.IGNORECASE)
                and not direct_retrieval_request(fresh, recent))


def grounded_search_target(target: str, fresh: str, recent: str) -> bool:
    """A weak score cannot revive an artifact solely from older context."""
    return bool((_ARTIFACT.search(fresh) and words(target) & words(fresh))
                or direct_retrieval_request(fresh, recent)
                or (_PAST_QUESTION.search(fresh) and _ARTIFACT.search(recent)))


def fresh_work_transition(fresh: str) -> bool:
    normalized = _speech(fresh)
    return bool((_WORK_TRANSITION.search(normalized) or _ACCEPT.search(normalized))
                and not _CANCEL.search(normalized) and not _HYPOTHETICAL.search(normalized))
