"""Smart Search (smart-search CLI) — Hermes web_search / web_extract bridge.

Hermes natively pins one backend per capability. This plugin turns the local
``smart-search`` CLI into a single backend whose insides fan out across whatever
providers you configured there (Exa / Tavily / TinyFish / Firecrawl / AnySearch
/ Jina / ...), with smart-search's own intent routing, same-capability fallback
and persisted provider cooldown.

Select it with::

    web:
      search_backend: "smartsearch"
      extract_backend: "smartsearch"

Env:
    SMARTSEARCH_CLI      path to the CLI (default: ``smart-search`` on PATH)
    SMARTSEARCH_TIMEOUT  per-call budget in seconds (default 90)

The plugin never stores keys itself: keys live in smart-search's own config
(``~/.config/smart-search/config.json``, written by ``smart-search setup`` /
``smart-search config set``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # Hermes core (always present when the plugin is loaded by Hermes)
    from agent.web_search_provider import WebSearchProvider
except ImportError:  # standalone import: tests, CI, tooling on a bare checkout
    class WebSearchProvider:  # minimal duck-typed stand-in
        """Fallback base so the module imports without Hermes installed.

        Hermes always supplies the real ABC; this only keeps linting/tests/CI
        working on a checkout that has no Hermes tree next to it.
        """

        def is_available(self) -> bool:
            return False

        def supports_search(self) -> bool:
            return True

        def supports_extract(self) -> bool:
            return False

        def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
            raise NotImplementedError

        def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
            raise NotImplementedError

logger = logging.getLogger(__name__)

_CLI_ENV = "SMARTSEARCH_CLI"
_TIMEOUT_ENV = "SMARTSEARCH_TIMEOUT"
_DEFAULT_TIMEOUT = 90
_MIN_TIMEOUT = 15
_MAX_TIMEOUT = 300

# Per-call ceilings for `smart-search research` (its own budgets, no --timeout flag).
_RESEARCH_BUDGETS = {"quick": 120, "standard": 240, "deep": 420}

# No provider list here on purpose: every channel smart-search knows (Exa,
# Tavily, TinyFish, Firecrawl, AnySearch, Jina, Zhipu, DeepSeek, …) is reached
# through the CLI, so adding a key in smart-search config needs no change here.

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FM_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)


# ── CLI plumbing ────────────────────────────────────────────────────────────

def _cli_path() -> str:
    """Resolved smart-search executable, or ``""`` when it is not reachable.

    Order: ``SMARTSEARCH_CLI`` override → ``PATH`` → the usual install spots
    (``/usr/local/bin``, ``~/.local/bin``, Homebrew, the profile-local bin) so the
    plugin still works when Hermes runs with a slimmer PATH than your shell.
    """
    configured = (os.getenv(_CLI_ENV) or "").strip()
    if configured:
        return configured if Path(configured).exists() else ""
    found = shutil.which("smart-search")
    if found:
        return found
    home = Path.home()
    candidates = (
        Path("/usr/local/bin/smart-search"),
        home / ".local" / "bin" / "smart-search",
        Path("/opt/homebrew/bin/smart-search"),
        Path("/usr/bin/smart-search"),
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return ""


def _launch_cmd(exe: str, args: List[str]) -> List[str]:
    """``[exe, *args]``, or a ``cmd /c`` prefix for Windows ``.cmd``/``.bat`` shims."""
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


def _budget() -> int:
    try:
        value = int(float(os.getenv(_TIMEOUT_ENV) or _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT
    return max(_MIN_TIMEOUT, min(value, _MAX_TIMEOUT))


def _run(args: List[str], *, timeout: int, pass_timeout: bool = True) -> Tuple[int, str, str]:
    """Run the CLI and return ``(exit_code, stdout, stderr)``; never raises.

    ``pass_timeout`` is False for subcommands without a ``--timeout`` flag
    (``research`` has its own budgets): the budget is handed over as
    ``SMART_SEARCH_TIMEOUT_SECONDS`` instead.
    """
    exe = _cli_path()
    if not exe:
        return 127, "", f"smart-search CLI not found (set {_CLI_ENV} or install it)"
    tail = ["--format", "json", *(["--timeout", str(timeout)] if pass_timeout else [])]
    cmd = _launch_cmd(exe, [*args, *tail])
    env = {**os.environ, "NO_COLOR": "1"}
    if not pass_timeout:
        env["SMART_SEARCH_TIMEOUT_SECONDS"] = str(timeout)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 25,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"smart-search exceeded the {timeout + 25}s process budget"
    except OSError as exc:
        return 126, "", f"could not start smart-search: {exc}"


def _load_json(stdout: str) -> Optional[Dict[str, Any]]:
    """Parse the CLI's JSON envelope, tolerating leading log lines."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _failure_text(payload: Dict[str, Any], stderr: str) -> str:
    """Compact, actionable error: error_type + message + degraded providers."""
    err_type = str(payload.get("error_type") or "").strip()
    err = str(payload.get("error") or "").strip()
    if not err:
        err = (stderr or "").strip().splitlines()[-1] if (stderr or "").strip() else "smart-search failed"
    head = f"smart-search {err_type}: {err}".strip() if err_type else f"smart-search: {err}"
    notices = payload.get("provider_notices") or []
    if notices:
        bits = []
        for notice in notices[:4]:
            if isinstance(notice, dict):
                bits.append(
                    " ".join(
                        str(notice.get(k) or "").strip()
                        for k in ("provider", "status", "error_type", "error")
                        if str(notice.get(k) or "").strip()
                    )
                )
            else:
                bits.append(str(notice))
        bits = [b for b in bits if b]
        if bits:
            head += f" | providers: {'; '.join(bits)}"
    return head


# ── Source normalisation ────────────────────────────────────────────────────

def _hit(title: str, url: str, description: str, position: int) -> Dict[str, Any]:
    return {"title": title or url, "url": url, "description": description, "position": position}


def _merge_sources(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """primary (citations) + extra (web) when present, else the flat ``sources`` list."""
    primary = payload.get("primary_sources") or []
    extra = payload.get("extra_sources") or []
    rows = [*primary, *extra] if (primary or extra) else (payload.get("sources") or [])
    return [r for r in rows if isinstance(r, dict)]


def _describe(item: Dict[str, Any]) -> str:
    desc = str(item.get("description") or "").strip()
    provider = str(item.get("provider") or "").strip()
    published = str(item.get("published_date") or "").strip()
    prefix = " ".join(p for p in (f"[{provider}]" if provider else "", published) if p)
    return f"{prefix} {desc}".strip() if prefix and desc else (prefix or desc)


def _title_from_markdown(content: str) -> str:
    head = content[:4000]
    match = _H1_RE.search(head) or _FM_TITLE_RE.search(head)
    return match.group(1).strip(' "\'') if match else ""


# ── Provider ────────────────────────────────────────────────────────────────

class SmartSearchProvider(WebSearchProvider):
    """One Hermes backend, many smart-search channels underneath."""

    @property
    def name(self) -> str:
        return "smartsearch"

    @property
    def display_name(self) -> str:
        return "Smart Search (multi-provider CLI)"

    def is_available(self) -> bool:
        # Cheap only: binary present. Missing provider keys surface as the CLI's
        # own config_error at call time (and Hermes' keyless rescue covers it).
        return bool(_cli_path())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 5), 20))
        budget = _budget()
        logger.info("smart-search search: '%s' (limit=%d, budget=%ds)", query, limit, budget)
        code, out, err = _run(["search", query], timeout=budget)
        payload = _load_json(out)
        if payload is None:
            detail = (err or out or "no output").strip()[:600]
            return {"success": False, "error": f"smart-search returned no JSON (exit {code}): {detail}"}
        if not payload.get("ok"):
            return {"success": False, "error": _failure_text(payload, err)}

        hits: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in _merge_sources(payload):
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            hits.append(_hit(str(item.get("title") or "").strip(), url, _describe(item), len(hits) + 1))
            if len(hits) >= limit:
                break

        if not hits:
            content = str(payload.get("content") or "").strip()
            if content:
                hits = [_hit("Smart Search answer", "", content[:2000], 1)]
            else:
                return {"success": False, "error": "smart-search returned no sources"}
        return {"success": True, "data": {"web": hits}}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        urls = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
        if not urls:
            return []
        workers = min(3, len(urls))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="smartsearch-fetch") as pool:
            return list(pool.map(self._fetch_one, urls))

    def _fetch_one(self, url: str) -> Dict[str, Any]:
        budget = _budget()
        code, out, err = _run(["fetch", url], timeout=budget)
        payload = _load_json(out)
        if payload is None:
            detail = (err or out or "no output").strip()[:400]
            return {"url": url, "title": "", "content": "", "error": f"smart-search fetch: {detail}"}
        if payload.get("ok"):
            content = str(payload.get("content") or "")
            if content.strip():
                return {
                    "url": url,
                    "title": _title_from_markdown(content),
                    "content": content,
                    "raw_content": content,
                    "metadata": {"sourceURL": url, "provider": payload.get("provider") or ""},
                }
            return {"url": url, "title": "", "content": "", "error": "smart-search fetch returned empty content"}
        return {"url": url, "title": "", "content": "", "error": _failure_text(payload, err)}

    def research(self, query: str, budget: str = "standard") -> Dict[str, Any]:
        """Bounded multi-round research: search → read pages → fill gaps → cite.

        Returns a compact result for the ``web_research`` tool; ``smart-search``
        keeps the expensive detail in its own evidence files.
        """
        budget = str(budget or "standard").strip().lower()
        if budget not in _RESEARCH_BUDGETS:
            budget = "standard"
        logger.info("smart-search research: '%s' (budget=%s)", query, budget)
        code, out, err = _run(["research", query, "--budget", budget], timeout=_RESEARCH_BUDGETS[budget], pass_timeout=False)
        payload = _load_json(out)
        if payload is None:
            detail = (err or out or "no output").strip()[:600]
            return {"success": False, "error": f"smart-search research returned no JSON (exit {code}): {detail}"}

        answer = str(payload.get("content") or payload.get("final_answer") or "").strip()
        ok = bool(payload.get("ok"))
        if not ok and not answer:
            return {"success": False, "error": _failure_text(payload, err)}

        evidence = payload.get("evidence_assessment") or {}
        citations = [
            {
                "title": str(c.get("title") or c.get("url") or "").strip(),
                "url": str(c.get("url") or "").strip(),
                "provider": str(c.get("provider") or "").strip(),
            }
            for c in (payload.get("citations") or [])
            if isinstance(c, dict) and c.get("url")
        ]
        warnings = list(payload.get("warnings") or [])
        if not ok:
            # Evidence bar not met (or a phase timed out) but findings exist: hand them
            # over as a partial result instead of a bare failure the model would retry.
            reason = str(payload.get("error") or "research ended before the evidence bar was met").strip()
            err_type = str(payload.get("error_type") or "").strip()
            warnings.append(f"{err_type}: {reason}" if err_type else reason)
        return {
            "success": True,
            "query": payload.get("query") or query,
            "budget": payload.get("budget") or budget,
            "answer": answer,
            "citations": citations,
            "evidence": {
                "status": evidence.get("status"),
                "sufficient": evidence.get("sufficient"),
                "gaps": evidence.get("gaps") or [],
            },
            "providers_used": payload.get("providers_used") or [],
            "rounds": len(payload.get("stage_results") or []),
            "partial_success": (not ok) or bool(payload.get("partial_success")),
            "warnings": warnings,
            "elapsed_ms": payload.get("elapsed_ms"),
        }

    def research_json(self, args: Dict[str, Any]) -> str:
        """``web_research`` tool handler: JSON string in, JSON string out."""
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"success": False, "error": "query is required"}, ensure_ascii=False)
        return json.dumps(self.research(query, str(args.get("budget") or "standard")), ensure_ascii=False)

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "cli",
            "tag": "Routes web_search/web_extract through the local smart-search CLI "
                   "(Exa, Tavily, TinyFish, Firecrawl, AnySearch …). Keys live in smart-search config.",
            "env_vars": [],
        }


RESEARCH_SCHEMA: Dict[str, Any] = {
    "name": "web_research",
    "description": (
        "Run a bounded multi-round web research task: search, read the discovered pages, fill evidence gaps, "
        "and return an evidence-backed answer with citations. Much slower and more expensive than web_search "
        "(tens of seconds up to a few minutes, paid provider quota) — use it for questions that need several "
        "sources compared or corroborated, not for quick lookups."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The research question."},
            "budget": {
                "type": "string",
                "enum": ["quick", "standard", "deep"],
                "description": "Depth ceiling: quick ≈ 2 rounds / ~45s, standard ≈ 3 rounds / ~120s, "
                               "deep ≈ 10 rounds / several minutes. Default: standard.",
            },
        },
        "required": ["query"],
    },
}

