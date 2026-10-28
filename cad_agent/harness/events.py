"""Public event vocabulary shared by execution, driver and tool pipeline."""
TOOL_TERMINAL = frozenset({'completed', 'failed', 'rejected', 'not_started', 'unknown'})
TOOL_STAGES = frozenset({'received', 'admitted', 'dispatched'}) | TOOL_TERMINAL
