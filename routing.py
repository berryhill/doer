"""Inspectable GPT-6-only routing policy; no provider entitlement discovery here."""

CANDIDATES = ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
              "gpt-6-astra-900k", "gpt-6-sol-900k", "gpt-6-luna-900k")
FAMILIES = CANDIDATES[:3]


class RoutingError(ValueError):
    def __init__(self, message, stages):
        super().__init__(message)
        self.stages = stages


def choose_model(task, available, context_bytes, choose_family,
                 *, override=None, policy="sol", observed_input_tokens=None,
                 availability_source="operator_confirmed_not_rechecked", min_confidence=0.6):
    """Return auditable stages; availability is caller-confirmed, never inferred."""
    if override is not None:
        return {"model": override, "reason": "explicit_override", "uncertainty": None,
                "stages": [{"stage": "override", "model": override,
                            "availability": "not_checked", "context_fit": "not_checked"}]}
    if policy not in ("sol", "rules", "laya"):
        raise ValueError("unknown routing policy")
    if not isinstance(context_bytes, int) or context_bytes < 0:
        raise ValueError("context_bytes must be an observed nonnegative byte count")
    if any(name not in CANDIDATES for name in available):
        raise ValueError("unapproved implementation candidate")
    available = tuple(dict.fromkeys(available))
    families = tuple(name for name in FAMILIES if name in available)
    if not families:
        raise ValueError("no confirmed normal-context GPT-6 candidate")
    stages = [{"stage": "availability", "source": availability_source,
               "available": list(available), "eligible_families": list(families),
               "unavailable": [n for n in CANDIDATES if n not in available]},
              {"stage": "capabilities", "constraint": "Hermes GPT-6 candidates only",
               "family_role_assumptions": "heuristic_not_verified"}]
    if observed_input_tokens is not None:
        if (not isinstance(observed_input_tokens, int) or observed_input_tokens < 0):
            raise ValueError("observed_input_tokens must be a nonnegative integer")
        if observed_input_tokens > 880000:
            raise ValueError("reported input exceeds conservative long-context allowance")
        fit = "long_context_indicated" if observed_input_tokens > 240000 else "normal_context_indicated"
        uncertainty = "operator_reported_token_count_not_verified"
    else:
        fit = "normal_context_likely" if context_bytes <= 200000 else "unknown"
        uncertainty = "bytes_are_not_tokens; future_tool_reads_unmeasured"
    stages.append({"stage": "context_fit", "context_bytes": context_bytes,
                   "observed_input_tokens": observed_input_tokens, "fit": fit,
                   "uncertainty": uncertainty})
    if fit == "unknown":
        raise RoutingError("context fit unknown: supply a measured token count or explicit --sol", stages)
    if fit == "long_context_indicated":
        families = tuple(f for f in families if f + "-900k" in available)
        if not families:
            raise RoutingError("no confirmed long-context alias for reported input", stages)
    if policy == "sol":
        if "gpt-6-sol" not in families:
            raise ValueError("Sol is not confirmed available for stable default")
        choice, confidence, reason = "gpt-6-sol", None, "stable_default"
        stages.append({"stage": "family_choice", "policy": "always_sol", "choice": choice})
    elif policy == "rules":
        risky = any(term in task.lower() for term in ("security", "tenant", "auth", "delete", "concurr"))
        choice = ("gpt-6-luna" if task.startswith("Create ") and len(task.encode("utf-8")) < 256 and
                  not risky and
                  "gpt-6-luna" in families else "gpt-6-sol")
        if choice not in families:
            raise ValueError("rules baseline has no confirmed safe candidate")
        confidence, reason = None, "deterministic_rules"
        stages.append({"stage": "family_choice", "policy": "rules", "choice": choice})
    else:
        raw = choose_family(task, families)
        raw = raw if isinstance(raw, dict) else {}
        picked, confidence = raw.get("choice"), raw.get("confidence")
        valid = (picked in families and isinstance(confidence, (int, float)) and
                 not isinstance(confidence, bool) and min_confidence <= confidence <= 1)
        choice = picked if valid else "gpt-6-sol"
        reason = ("laya_choice" if valid else
                  "unavailable_choice" if picked not in families else "uncertain_family")
        stages.append({"stage": "family_choice", "policy": "laya", "eligible": list(families),
                       "choice": picked, "confidence": confidence,
                       "confidence_kind": "uncalibrated", "threshold": min_confidence,
                       "reason": reason})
        if choice not in families:
            raise ValueError("uncertain routing with no confirmed Sol fallback")
    model = choice + ("-900k" if fit == "long_context_indicated" else "")
    stages.append({"stage": "selected", "family": choice, "model": model,
                   "alias_reason": fit if model.endswith("-900k") else "not_demonstrated",
                   "reason": reason})
    return {"model": model, "reason": reason, "uncertainty": uncertainty,
            "stages": stages}
