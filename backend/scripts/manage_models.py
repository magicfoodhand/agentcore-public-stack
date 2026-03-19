#!/usr/bin/env python3
"""
Interactive TUI for managing Bedrock models in the AgentCore DynamoDB table.

Connects to AWS to:
  - List models currently registered in DynamoDB
  - Search available Bedrock foundation models and inference profiles
  - Fetch live pricing from the AWS Pricing API
  - Add Bedrock models to the DynamoDB managed-models table
  - Toggle models enabled/disabled, set default, remove

Environment variables:
    DDB_MANAGED_MODELS_TABLE  - DynamoDB table name  (required)
    AWS_REGION                - AWS region            (default: us-east-1)
    AWS_PROFILE               - AWS profile           (optional)

Usage:
    python manage_models.py
    DDB_MANAGED_MODELS_TABLE=my-table AWS_REGION=us-west-2 python manage_models.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

# ── Constants ───────────────────────────────────────────────────────────────────

MODEL_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# ANSI helpers
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"
UNDERLINE = "\033[4m"


def clr(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET


# ── AWS helpers ─────────────────────────────────────────────────────────────────


def get_session(region: str) -> boto3.Session:
    profile = os.environ.get("AWS_PROFILE")
    kwargs: dict[str, str] = {"region_name": region}
    if profile:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def get_table(session: boto3.Session, table_name: str):
    dynamodb = session.resource("dynamodb")
    return dynamodb.Table(table_name)


# ── AWS Pricing API ─────────────────────────────────────────────────────────────

# Cache so we only hit the Pricing API once per session
_pricing_cache: dict[str, dict[str, Decimal]] | None = None


def _fetch_all_pricing(session: boto3.Session, region: str) -> dict[str, dict[str, Decimal]]:
    """Fetch on-demand inference pricing for all Bedrock models from the AWS Pricing API.

    Returns a dict keyed by the Pricing API model name (e.g. "Nova Lite") with
    sub-keys: input, output, cacheWrite, cacheRead  (per 1M tokens as Decimal).
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache

    pricing_client = session.client("pricing", region_name="us-east-1")
    result: dict[str, dict[str, Decimal]] = {}

    # Map inferenceType values from the Pricing API to our internal keys
    inference_type_map = {
        "Input tokens": "input",
        "Output tokens": "output",
        "Prompt cache write input tokens": "cacheWrite",
        "Prompt cache read input tokens": "cacheRead",
    }

    for inf_type_api, price_key in inference_type_map.items():
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "ServiceCode": "AmazonBedrock",
                "Filters": [
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                    {"Type": "TERM_MATCH", "Field": "inferenceType", "Value": inf_type_api},
                    {"Type": "TERM_MATCH", "Field": "feature", "Value": "On-demand Inference"},
                ],
                "MaxResults": 100,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            try:
                resp = pricing_client.get_products(**kwargs)
            except ClientError:
                break

            for pl in resp.get("PriceList", []):
                data = json.loads(pl)
                attrs = data.get("product", {}).get("attributes", {})
                model_name = attrs.get("model", "")
                if not model_name:
                    continue

                terms = data.get("terms", {}).get("OnDemand", {})
                for term in terms.values():
                    for dim in term.get("priceDimensions", {}).values():
                        usd = dim.get("pricePerUnit", {}).get("USD")
                        if usd is not None:
                            per_1k = Decimal(usd)
                            per_1m = per_1k * 1000  # API gives per-1K; we store per-1M
                            entry = result.setdefault(model_name, {})
                            entry[price_key] = per_1m

            next_token = resp.get("NextToken")
            if not next_token:
                break

    _pricing_cache = result
    return result


def lookup_pricing(
    session: boto3.Session,
    region: str,
    model_display_name: str,
) -> dict[str, Decimal]:
    """Look up pricing for a model by its display name from the Bedrock ListFoundationModels API.

    Returns dict with keys: input, output, cacheWrite, cacheRead  (per 1M tokens).
    Missing keys default to Decimal("0").
    """
    all_pricing = _fetch_all_pricing(session, region)
    pricing = all_pricing.get(model_display_name, {})
    return {
        "input": pricing.get("input", Decimal("0")),
        "output": pricing.get("output", Decimal("0")),
        "cacheWrite": pricing.get("cacheWrite", Decimal("0")),
        "cacheRead": pricing.get("cacheRead", Decimal("0")),
    }


# ── Bedrock API ─────────────────────────────────────────────────────────────────


def fetch_bedrock_models(session: boto3.Session) -> list[dict[str, Any]]:
    """List foundation models from the Bedrock API."""
    client = session.client("bedrock")
    models: list[dict[str, Any]] = []
    try:
        resp = client.list_foundation_models()
        for m in resp.get("modelSummaries", []):
            models.append(m)
    except ClientError as e:
        print(clr(f"\n  Error listing Bedrock models: {e}", RED))
    except Exception as e:
        print(clr(f"\n  Unexpected error: {e}", RED))
    models.sort(key=lambda m: (m.get("providerName", ""), m.get("modelName", "")))
    return models


def fetch_inference_profiles(session: boto3.Session) -> list[dict[str, Any]]:
    """List cross-region inference profiles (global.*, us.*, eu.* IDs)."""
    client = session.client("bedrock")
    profiles: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"maxResults": 100}
    try:
        while True:
            resp = client.list_inference_profiles(**kwargs)
            profiles.extend(resp.get("inferenceProfileSummaries", []))
            nt = resp.get("nextToken")
            if not nt:
                break
            kwargs["nextToken"] = nt
    except ClientError as e:
        print(clr(f"\n  Error listing inference profiles: {e}", RED))
    except Exception as e:
        print(clr(f"\n  Unexpected error: {e}", RED))
    profiles.sort(key=lambda p: p.get("inferenceProfileId", ""))
    return profiles


# ── DynamoDB operations ────────────────────────────────────────────────────────


def fetch_current_models(table) -> list[dict[str, Any]]:
    """Scan for all MODEL# items in the table."""
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": Key("PK").begins_with("MODEL#"),
    }
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    items.sort(key=lambda m: (m.get("providerName", ""), m.get("modelName", "")))
    return items


def write_model_to_dynamo(table, model_def: dict[str, Any]) -> str:
    """Write a model item to DynamoDB. Returns status message."""
    model_id = model_def["modelId"]
    deterministic_uuid = str(uuid.uuid5(MODEL_UUID_NAMESPACE, model_id))

    # Check existence via GSI
    try:
        query_resp = table.query(
            IndexName="ModelIdIndex",
            KeyConditionExpression=Key("GSI1PK").eq(f"MODEL#{model_id}"),
            Limit=1,
        )
        if query_resp.get("Items"):
            return "already exists — skipped"
    except ClientError as e:
        return f"error checking existence: {e}"

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    pk = f"MODEL#{deterministic_uuid}"

    item: dict[str, Any] = {
        "PK": pk,
        "SK": pk,
        "GSI1PK": f"MODEL#{model_id}",
        "GSI1SK": pk,
        "id": deterministic_uuid,
        "modelId": model_id,
        "modelName": model_def.get("modelName", model_id),
        "provider": model_def.get("provider", "bedrock"),
        "providerName": model_def.get("providerName", "Unknown"),
        "inputModalities": model_def.get("inputModalities", ["TEXT"]),
        "outputModalities": model_def.get("outputModalities", ["TEXT"]),
        "maxInputTokens": model_def.get("maxInputTokens", 0),
        "maxOutputTokens": model_def.get("maxOutputTokens", 0),
        "allowedAppRoles": [],
        "availableToRoles": [],
        "enabled": True,
        "inputPricePerMillionTokens": model_def.get("inputPricePerMillionTokens", Decimal("0")),
        "outputPricePerMillionTokens": model_def.get("outputPricePerMillionTokens", Decimal("0")),
        "cacheWritePricePerMillionTokens": model_def.get("cacheWritePricePerMillionTokens", Decimal("0")),
        "cacheReadPricePerMillionTokens": model_def.get("cacheReadPricePerMillionTokens", Decimal("0")),
        "isReasoningModel": model_def.get("isReasoningModel", False),
        "supportsCaching": model_def.get("supportsCaching", False),
        "isDefault": False,
        "createdAt": now,
        "updatedAt": now,
    }

    try:
        table.put_item(Item=item)
        return "added successfully"
    except ClientError as e:
        return f"error writing: {e}"


def delete_model_from_dynamo(table, model_item: dict[str, Any]) -> str:
    """Delete a model item from DynamoDB."""
    try:
        table.delete_item(Key={"PK": model_item["PK"], "SK": model_item["SK"]})
        return "deleted"
    except ClientError as e:
        return f"error: {e}"


def toggle_model_enabled(table, model_item: dict[str, Any]) -> str:
    """Toggle the enabled flag on a model."""
    new_val = not model_item.get("enabled", True)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        table.update_item(
            Key={"PK": model_item["PK"], "SK": model_item["SK"]},
            UpdateExpression="SET enabled = :e, updatedAt = :u",
            ExpressionAttributeValues={":e": new_val, ":u": now},
        )
        return "enabled" if new_val else "disabled"
    except ClientError as e:
        return f"error: {e}"


def set_model_default(table, model_item: dict[str, Any], all_models: list[dict]) -> str:
    """Set a model as the default (unsets all others)."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        for m in all_models:
            if m.get("isDefault", False):
                table.update_item(
                    Key={"PK": m["PK"], "SK": m["SK"]},
                    UpdateExpression="SET isDefault = :d, updatedAt = :u",
                    ExpressionAttributeValues={":d": False, ":u": now},
                )
        table.update_item(
            Key={"PK": model_item["PK"], "SK": model_item["SK"]},
            UpdateExpression="SET isDefault = :d, updatedAt = :u",
            ExpressionAttributeValues={":d": True, ":u": now},
        )
        return "set as default"
    except ClientError as e:
        return f"error: {e}"


# ── Display / prompt helpers ────────────────────────────────────────────────────

HEADER_LINE = "─" * 100


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_banner():
    print()
    print(clr("  ╔══════════════════════════════════════════════════════╗", CYAN))
    print(clr("  ║        AgentCore Model Manager                      ║", CYAN))
    print(clr("  ╚══════════════════════════════════════════════════════╝", CYAN))
    print()


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {text}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return val or default


def prompt_confirm(text: str, default_yes: bool = False) -> bool:
    hint = "Y/n" if default_yes else "y/N"
    val = prompt(f"{text} ({hint})", "")
    if not val:
        return default_yes
    return val.lower().startswith("y")


def prompt_int(text: str, min_val: int, max_val: int) -> int | None:
    val = prompt(text)
    if not val:
        return None
    try:
        n = int(val)
        if min_val <= n <= max_val:
            return n
    except ValueError:
        pass
    print(clr(f"  Invalid input. Enter a number between {min_val} and {max_val}.", RED))
    return None


def prompt_decimal(text: str, default: str = "0") -> Decimal:
    val = prompt(text, default)
    try:
        return Decimal(val)
    except Exception:
        print(clr("  Invalid number, using 0.", YELLOW))
        return Decimal("0")


def fmt_price(d: Decimal) -> str:
    """Format a Decimal price for display."""
    if d == 0:
        return clr("—", DIM)
    return f"${d:,.4f}"


# ── Table printers ──────────────────────────────────────────────────────────────


def print_model_table(models: list[dict[str, Any]], show_index: bool = True):
    """Print a formatted table of registered DynamoDB models."""
    if not models:
        print(clr("  No models found.", DIM))
        return

    idx_col = "  #   " if show_index else "  "
    print(
        clr(
            f"{idx_col}"
            f"{'Model Name':<30s} "
            f"{'Model ID':<48s} "
            f"{'Provider':<12s} "
            f"{'Status':<10s}",
            BOLD,
        )
    )
    print(clr(f"  {HEADER_LINE}", DIM))

    for i, m in enumerate(models, 1):
        name = m.get("modelName", "—")[:28]
        mid = m.get("modelId", "—")[:46]
        provider = m.get("providerName", "—")[:10]
        enabled = m.get("enabled", True)
        is_default = m.get("isDefault", False)

        status_parts: list[str] = []
        status_parts.append(clr("on", GREEN) if enabled else clr("off", RED))
        if is_default:
            status_parts.append(clr("*default", YELLOW))

        idx = f"  {i:<4d}" if show_index else "  "
        print(f"{idx}{name:<30s} {mid:<48s} {provider:<12s} {' '.join(status_parts)}")

    print(clr(f"  {HEADER_LINE}", DIM))
    print(clr(f"  {len(models)} model(s)", DIM))


def print_bedrock_table(
    models: list[dict[str, Any]],
    registered_ids: set[str],
):
    """Print Bedrock foundation models / inference profiles with registration status."""
    if not models:
        print(clr("  No models found.", DIM))
        return

    print(
        clr(
            f"  {'#':<5s}"
            f"{'Model Name':<35s} "
            f"{'Model ID':<55s} "
            f"{'Provider':<14s} "
            f"{'Status':<8s} "
            f"{'Reg?':<6s}",
            BOLD,
        )
    )
    print(clr(f"  {HEADER_LINE}", DIM))

    for i, m in enumerate(models, 1):
        name = m.get("modelName", m.get("inferenceProfileName", "—"))[:33]
        mid = m.get("modelId", m.get("inferenceProfileId", "—"))
        mid_display = mid[:53]
        provider = m.get("providerName", "—")[:12]
        status = m.get("modelLifecycle", {}).get("status", m.get("status", "—"))[:6]
        is_registered = mid in registered_ids

        status_str = clr(status, GREEN) if status == "ACTIVE" else clr(status, DIM)
        reg_str = clr("yes", GREEN) if is_registered else clr("—", DIM)
        print(f"  {i:<5d}{name:<35s} {mid_display:<55s} {provider:<14s} {status_str:<17s} {reg_str}")

    print(clr(f"  {HEADER_LINE}", DIM))
    print(clr(f"  {len(models)} model(s)", DIM))


# ── TUI Screens ─────────────────────────────────────────────────────────────────


def screen_list_models(table):
    """Show all registered models with management actions."""
    clear_screen()
    print_banner()
    print(clr("  Current Registered Models", BOLD, UNDERLINE))
    print()

    models = fetch_current_models(table)
    print_model_table(models)
    print()

    if not models:
        prompt("Press Enter to return")
        return

    print(clr("  Actions:  [t]oggle enabled  [d]efault  [r]emove  [Enter] back", DIM))
    action = prompt("Action").lower()

    if action == "t":
        idx = prompt_int("Model #", 1, len(models))
        if idx is not None:
            result = toggle_model_enabled(table, models[idx - 1])
            name = models[idx - 1].get("modelName", "?")
            print(clr(f"\n  {name}: {result}", GREEN if "error" not in result else RED))
            prompt("Press Enter to continue")

    elif action == "d":
        idx = prompt_int("Set default — Model #", 1, len(models))
        if idx is not None:
            result = set_model_default(table, models[idx - 1], models)
            name = models[idx - 1].get("modelName", "?")
            print(clr(f"\n  {name}: {result}", GREEN if "error" not in result else RED))
            prompt("Press Enter to continue")

    elif action == "r":
        idx = prompt_int("Remove — Model #", 1, len(models))
        if idx is not None:
            name = models[idx - 1].get("modelName", "?")
            mid = models[idx - 1].get("modelId", "?")
            if prompt_confirm(f"Delete '{name}' ({mid})?"):
                result = delete_model_from_dynamo(table, models[idx - 1])
                print(clr(f"\n  {name}: {result}", GREEN if "error" not in result else RED))
                prompt("Press Enter to continue")


def screen_search_and_add(session: boto3.Session, table, region: str):
    """Search Bedrock foundation models + inference profiles and add them."""
    clear_screen()
    print_banner()
    print(clr("  Search Bedrock Models", BOLD, UNDERLINE))
    print()

    # Let user choose the source
    print(clr("  Source:", DIM))
    print(clr("    1) Foundation models          — base model IDs from Bedrock", DIM))
    print(clr("    2) Inference profiles          — cross-region IDs (global.*, us.*, eu.*)", DIM))
    print(clr("    3) Both                        — combined list", DIM))
    source_choice = prompt("Source", "3")

    print(clr("  Fetching from Bedrock API...", DIM))

    # Always fetch inference profiles so we can validate prefixed IDs
    all_profiles = fetch_inference_profiles(session)
    valid_profile_ids: set[str] = {p.get("inferenceProfileId", "") for p in all_profiles}

    # Build a lookup from base foundation model ID -> set of available profile IDs
    # e.g. "anthropic.claude-sonnet-4-6" -> {"global.anthropic.claude-sonnet-4-6", "us.anthropic.claude-sonnet-4-6"}
    base_to_profiles: dict[str, set[str]] = {}
    for pid in valid_profile_ids:
        for pfx in ("global.", "us.", "eu.", "ap."):
            if pid.startswith(pfx):
                base = pid[len(pfx):]
                base_to_profiles.setdefault(base, set()).add(pid)
                break

    combined: list[dict[str, Any]] = []
    if source_choice in ("1", "3"):
        combined.extend(fetch_bedrock_models(session))
    if source_choice in ("2", "3"):
        # Normalise inference profile fields to match foundation model shape
        for p in all_profiles:
            combined.append({
                "modelId": p.get("inferenceProfileId", ""),
                "modelName": p.get("inferenceProfileName", ""),
                "providerName": _infer_provider(p.get("inferenceProfileId", "")),
                "inputModalities": ["TEXT"],  # profiles don't expose modalities
                "outputModalities": ["TEXT"],
                "modelLifecycle": {"status": p.get("status", "ACTIVE")},
                "_source": "profile",
                "_profileModels": p.get("models", []),
            })

    if not combined:
        prompt("No models returned. Press Enter to return")
        return

    # De-duplicate by modelId (profiles may overlap with foundation models)
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for m in combined:
        mid = m.get("modelId", "")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            deduped.append(m)
    deduped.sort(key=lambda m: (m.get("providerName", ""), m.get("modelName", "")))

    # Gather registered model IDs for status display
    current = fetch_current_models(table)
    registered_ids = {m.get("modelId", "") for m in current}

    # Filter
    filter_text = prompt("Filter (provider, name, or ID — blank for all)").lower()
    if filter_text:
        filtered = [
            m for m in deduped
            if filter_text in m.get("modelName", "").lower()
            or filter_text in m.get("modelId", "").lower()
            or filter_text in m.get("providerName", "").lower()
        ]
    else:
        filtered = deduped

    if not filtered:
        print(clr(f"\n  No models matching '{filter_text}'.", YELLOW))
        prompt("Press Enter to return")
        return

    clear_screen()
    print_banner()
    print(clr(f"  Bedrock Models ({len(filtered)} results)", BOLD, UNDERLINE))
    print()
    print_bedrock_table(filtered, registered_ids)
    print()

    # Selection
    print(clr("  Enter model number(s) to add (comma-separated), 'all' for unregistered, or blank to cancel.", DIM))
    selection = prompt("Add models").strip()
    if not selection:
        return

    indices: list[int] = []
    if selection.lower() == "all":
        for i, m in enumerate(filtered):
            if m.get("modelId", "") not in registered_ids:
                indices.append(i)
        if not indices:
            print(clr("\n  All displayed models are already registered.", YELLOW))
            prompt("Press Enter to return")
            return
    else:
        for part in selection.split(","):
            try:
                idx = int(part.strip()) - 1
                if 0 <= idx < len(filtered):
                    indices.append(idx)
            except ValueError:
                pass

    if not indices:
        print(clr("\n  No valid selections.", YELLOW))
        prompt("Press Enter to return")
        return

    # For foundation models (not already prefixed), ask about prefix
    has_base_ids = any(
        not filtered[i].get("modelId", "").startswith(("global.", "us.", "eu.", "ap."))
        for i in indices
    )
    prefix = ""
    if has_base_ids:
        print()
        print(clr("  Some selected models are base IDs. Add a cross-region prefix?", DIM))
        print(clr("  Only models that have a matching inference profile will be prefixed.", DIM))
        print(clr("  Models without a profile will be added with their base ID.", DIM))
        print()
        print(clr("    1) global.  — Cross-region inference (recommended)", DIM))
        print(clr("    2) us.      — US cross-region", DIM))
        print(clr("    3) eu.      — EU cross-region", DIM))
        print(clr("    4) (none)   — Use model ID as-is", DIM))
        prefix_choice = prompt("Prefix", "1")
        prefix = {"1": "global.", "2": "us.", "3": "eu.", "4": ""}.get(prefix_choice, "global.")

    # Fetch pricing from AWS Pricing API
    print()
    print(clr("  Fetching pricing from AWS Pricing API...", DIM))
    pricing_data = _fetch_all_pricing(session, region)
    pricing_model_names = set(pricing_data.keys())
    print(clr(f"  Pricing data available for {len(pricing_model_names)} models.", DIM))
    print()

    for idx in indices:
        bedrock = filtered[idx]
        raw_id = bedrock.get("modelId", "")
        display_name = bedrock.get("modelName", raw_id)

        # Determine the final model ID to store
        if raw_id.startswith(("global.", "us.", "eu.", "ap.")):
            # Already a profile ID — use as-is (it came from ListInferenceProfiles)
            final_id = raw_id
        elif prefix:
            # User wants a prefix — but only apply if the profile actually exists
            candidate = f"{prefix}{raw_id}"
            if candidate in valid_profile_ids:
                final_id = candidate
            else:
                # Check if ANY profile exists for this base model
                available = base_to_profiles.get(raw_id, set())
                if available:
                    print(clr(
                        f"  {display_name}: '{candidate}' does not exist. "
                        f"Available profiles: {', '.join(sorted(available))}",
                        YELLOW,
                    ))
                    alt = prompt(f"  Use one of these instead? (enter ID, or blank to use base '{raw_id}')")
                    final_id = alt if alt in valid_profile_ids else raw_id
                else:
                    print(clr(
                        f"  {display_name}: no inference profile exists — using base ID '{raw_id}'",
                        YELLOW,
                    ))
                    final_id = raw_id
        else:
            final_id = raw_id

        # Look up pricing by the model display name
        pricing = lookup_pricing(session, region, display_name)
        has_pricing = any(v > 0 for v in pricing.values())

        # Resolve modalities — for profiles, try to inherit from the foundation model
        in_mod = bedrock.get("inputModalities", ["TEXT"])
        out_mod = bedrock.get("outputModalities", ["TEXT"])

        # Infer caching support from provider
        provider_name = bedrock.get("providerName", "")
        supports_caching = (
            "anthropic" in provider_name.lower()
            or pricing.get("cacheWrite", Decimal("0")) > 0
            or pricing.get("cacheRead", Decimal("0")) > 0
        )

        if not has_pricing:
            print(clr(f"  {display_name}: no pricing found in AWS Pricing API.", YELLOW))
            if prompt_confirm("  Enter pricing manually?"):
                pricing["input"] = prompt_decimal("    Input $/M tokens")
                pricing["output"] = prompt_decimal("    Output $/M tokens")
                pricing["cacheWrite"] = prompt_decimal("    Cache write $/M tokens")
                pricing["cacheRead"] = prompt_decimal("    Cache read $/M tokens")
            else:
                print(clr("    Pricing set to $0 — you can update later.", DIM))

        model_def: dict[str, Any] = {
            "modelId": final_id,
            "modelName": display_name,
            "provider": "bedrock",
            "providerName": provider_name,
            "inputModalities": in_mod,
            "outputModalities": out_mod,
            "maxInputTokens": 0,
            "maxOutputTokens": 0,
            "inputPricePerMillionTokens": pricing["input"],
            "outputPricePerMillionTokens": pricing["output"],
            "cacheWritePricePerMillionTokens": pricing["cacheWrite"],
            "cacheReadPricePerMillionTokens": pricing["cacheRead"],
            "isReasoningModel": False,
            "supportsCaching": supports_caching,
        }

        result = write_model_to_dynamo(table, model_def)
        color = GREEN if "success" in result else YELLOW if "skipped" in result else RED
        price_info = ""
        if has_pricing:
            price_info = (
                f"  [in={fmt_price(pricing['input'])} out={fmt_price(pricing['output'])}"
                f" cw={fmt_price(pricing['cacheWrite'])} cr={fmt_price(pricing['cacheRead'])}]"
            )
        print(clr(f"  {display_name} ({final_id}): {result}{price_info}", color))

    print()
    prompt("Press Enter to continue")


def screen_add_custom(session: boto3.Session, table, region: str):
    """Manually add a model with custom parameters."""
    clear_screen()
    print_banner()
    print(clr("  Add Custom Model", BOLD, UNDERLINE))
    print()

    model_id = prompt("Model ID (e.g. global.anthropic.claude-sonnet-4-6)")
    if not model_id:
        return

    model_name = prompt("Display name", model_id.split(".")[-1])
    provider_name = prompt("Provider name (e.g. Anthropic, Meta, Amazon)", "Unknown")

    print()
    print(clr("  Input modalities (comma-separated): TEXT, IMAGE, VIDEO, AUDIO", DIM))
    in_mod = [m.strip() for m in prompt("Input modalities", "TEXT").upper().split(",") if m.strip()]
    out_mod = [m.strip() for m in prompt("Output modalities", "TEXT").upper().split(",") if m.strip()]

    max_in = int(prompt("Max input tokens", "200000") or "200000")
    max_out = int(prompt("Max output tokens", "4096") or "4096")

    # Try to auto-fill pricing from AWS
    print()
    print(clr("  Looking up pricing from AWS Pricing API...", DIM))
    pricing = lookup_pricing(session, region, model_name)
    has_pricing = any(v > 0 for v in pricing.values())

    if has_pricing:
        print(clr(f"  Found pricing: in={fmt_price(pricing['input'])} "
                   f"out={fmt_price(pricing['output'])} "
                   f"cw={fmt_price(pricing['cacheWrite'])} "
                   f"cr={fmt_price(pricing['cacheRead'])}", GREEN))
        if not prompt_confirm("  Use this pricing?", default_yes=True):
            has_pricing = False  # fall through to manual
    else:
        print(clr("  No pricing found for this model name.", YELLOW))

    if not has_pricing:
        print(clr("  Pricing (per million tokens):", DIM))
        pricing["input"] = prompt_decimal("Input price", "0")
        pricing["output"] = prompt_decimal("Output price", "0")
        pricing["cacheWrite"] = prompt_decimal("Cache write price", "0")
        pricing["cacheRead"] = prompt_decimal("Cache read price", "0")

    is_reasoning = prompt_confirm("Is reasoning model?")
    supports_cache = prompt_confirm("Supports caching?")

    model_def: dict[str, Any] = {
        "modelId": model_id,
        "modelName": model_name,
        "provider": "bedrock",
        "providerName": provider_name,
        "inputModalities": in_mod,
        "outputModalities": out_mod,
        "maxInputTokens": max_in,
        "maxOutputTokens": max_out,
        "inputPricePerMillionTokens": pricing["input"],
        "outputPricePerMillionTokens": pricing["output"],
        "cacheWritePricePerMillionTokens": pricing["cacheWrite"],
        "cacheReadPricePerMillionTokens": pricing["cacheRead"],
        "isReasoningModel": is_reasoning,
        "supportsCaching": supports_cache,
    }

    print()
    if prompt_confirm(f"Add '{model_name}' ({model_id})?", default_yes=True):
        result = write_model_to_dynamo(table, model_def)
        color = GREEN if "success" in result else YELLOW if "skipped" in result else RED
        print(clr(f"\n  {model_name}: {result}", color))
    else:
        print(clr("\n  Cancelled.", DIM))

    prompt("Press Enter to continue")


def screen_model_detail(table):
    """View detailed info for a single model."""
    clear_screen()
    print_banner()
    print(clr("  Model Details", BOLD, UNDERLINE))
    print()

    models = fetch_current_models(table)
    print_model_table(models)
    print()

    if not models:
        prompt("Press Enter to return")
        return

    idx = prompt_int("Model # to inspect", 1, len(models))
    if idx is None:
        return

    m = models[idx - 1]
    print()
    print(clr(f"  ┌─ {m.get('modelName', '?')} {'─' * 55}", CYAN))
    fields = [
        ("Model ID", "modelId"),
        ("Provider", "providerName"),
        ("Enabled", "enabled"),
        ("Default", "isDefault"),
        ("Input Modalities", "inputModalities"),
        ("Output Modalities", "outputModalities"),
        ("Max Input Tokens", "maxInputTokens"),
        ("Max Output Tokens", "maxOutputTokens"),
        ("Input $/M tokens", "inputPricePerMillionTokens"),
        ("Output $/M tokens", "outputPricePerMillionTokens"),
        ("Cache Write $/M", "cacheWritePricePerMillionTokens"),
        ("Cache Read $/M", "cacheReadPricePerMillionTokens"),
        ("Reasoning Model", "isReasoningModel"),
        ("Supports Caching", "supportsCaching"),
        ("Created At", "createdAt"),
        ("Updated At", "updatedAt"),
        ("UUID", "id"),
    ]
    for label, key in fields:
        val = m.get(key, "—")
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val)
        elif isinstance(val, bool):
            val = clr("yes", GREEN) if val else clr("no", RED)
        elif isinstance(val, Decimal):
            val = fmt_price(val)
        print(f"  │ {label:<24s} {val}")
    print(clr(f"  └{'─' * 60}", CYAN))
    print()
    prompt("Press Enter to return")


# ── Utility ─────────────────────────────────────────────────────────────────────


def _infer_provider(profile_id: str) -> str:
    """Infer provider name from an inference profile ID like 'global.anthropic.claude-...'."""
    parts = profile_id.split(".")
    if len(parts) >= 2:
        raw = parts[1] if parts[0] in ("global", "us", "eu", "ap") else parts[0]
        return raw.capitalize()
    return "Unknown"


# ── Main loop ───────────────────────────────────────────────────────────────────


def main_menu():
    """Top-level TUI entry point."""
    table_name = os.environ.get("DDB_MANAGED_MODELS_TABLE", "")
    region = os.environ.get("AWS_REGION", "us-east-1")

    if not table_name:
        print(clr("\n  Error: DDB_MANAGED_MODELS_TABLE environment variable is required.", RED))
        print(clr("  Usage: DDB_MANAGED_MODELS_TABLE=<table-name> python manage_models.py\n", DIM))
        sys.exit(1)

    try:
        session = get_session(region)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account = identity["Account"]
    except (NoCredentialsError, ProfileNotFound, ClientError) as e:
        print(clr(f"\n  AWS credential error: {e}", RED))
        print(clr("  Ensure valid AWS credentials are configured.\n", DIM))
        sys.exit(1)

    table = get_table(session, table_name)

    while True:
        clear_screen()
        print_banner()
        print(clr(f"  Table:   {table_name}", DIM))
        print(clr(f"  Region:  {region}", DIM))
        print(clr(f"  Account: {account}", DIM))
        print()

        models = fetch_current_models(table)
        enabled = sum(1 for m in models if m.get("enabled", True))
        default_m = next((m for m in models if m.get("isDefault")), None)
        default_name = default_m.get("modelName", "—") if default_m else "none"

        print(clr(f"  Models: {len(models)} registered, {enabled} enabled, default: {default_name}", WHITE))
        print()
        print(clr("  ┌──────────────────────────────────────┐", CYAN))
        print(clr("  │", CYAN) + clr("  1 ", BOLD) + "List / manage registered models  " + clr("│", CYAN))
        print(clr("  │", CYAN) + clr("  2 ", BOLD) + "Search Bedrock & add models      " + clr("│", CYAN))
        print(clr("  │", CYAN) + clr("  3 ", BOLD) + "Add custom model manually        " + clr("│", CYAN))
        print(clr("  │", CYAN) + clr("  4 ", BOLD) + "View model details               " + clr("│", CYAN))
        print(clr("  │", CYAN) + clr("  q ", BOLD) + "Quit                             " + clr("│", CYAN))
        print(clr("  └──────────────────────────────────────┘", CYAN))
        print()

        choice = prompt("Choose").strip().lower()

        if choice == "1":
            screen_list_models(table)
        elif choice == "2":
            screen_search_and_add(session, table, region)
        elif choice == "3":
            screen_add_custom(session, table, region)
        elif choice == "4":
            screen_model_detail(table)
        elif choice in ("q", "quit", "exit"):
            clear_screen()
            print(clr("\n  Goodbye.\n", DIM))
            break


if __name__ == "__main__":
    main_menu()
