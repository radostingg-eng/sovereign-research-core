"""Host-input schema capabilities shared across runtime validators."""

CURRENT_FULL_CYCLE_SCHEMA_VERSION = 4
SUPPORTED_FULL_CYCLE_VERSIONS = frozenset({2, 3, 4})
STRUCTURED_FULL_CYCLE_VERSIONS = frozenset({3, 4})
CANONICAL_STAGED_INPUT_VERSIONS = frozenset({
    CURRENT_FULL_CYCLE_SCHEMA_VERSION,
})
