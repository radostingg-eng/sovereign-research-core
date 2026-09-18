# Sovereign Active Brain

This file is the compact working-memory surface for the Sovereign Investment System. It is intentionally bounded and must not become a transcript or raw archive.

## Admission rule

Only information that can materially affect current or recurring decisions belongs here:

- current portfolio/exposure facts that are useful across decisions;
- active theses and their current falsifiers;
- promoted strategies and the conditions under which they remain valid;
- active experiments and their evaluation status;
- validated reusable lessons;
- recurring failure modes and unresolved high-value hypotheses;
- current research-routing lessons;
- current goals that affect the workflow.

Each entry must reference its source memory IDs and version. Historical detail remains retrievable from Research Memory or the Raw Archive.

## Format

Each active entry should contain:

`memory_id`, `claim`, `type`, `status`, `as_of`, `source_ids`, `confidence`, `why_active`, `supersedes`, `brain_version`, `reconstruction_status`.

## Current bootstrap

The system is newly adopting the bounded-brain architecture. Existing GitHub state, theses, recommendations, lessons, experiments and audit records remain the source corpus. The first production distillation pass must build the initial Active Brain from those sources without importing ex-post information into historical ex-ante records.

## Capacity policy

The host owns the semantic selection. The deterministic runtime verifies only declared limits and structure. When capacity is reached, the host should replace lower-value active entries with newer or better-validated representations while preserving the displaced material in Research Memory or the Raw Archive.

## Integrity rule

Nothing in this file can supersede raw evidence. A brain entry is a view over source records, never the authoritative historical record itself.
