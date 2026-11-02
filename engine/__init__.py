"""S.E.N.T.I.A.L. engine package.

The engine layer is framework-free Python so it can be driven by the PySide6 UI,
by tests, or by future entry points without importing Qt.

Phase 1 exports the centralized tool registry:

* :class:`ToolRegistry` — registration, schema generation, validation,
  permission enforcement and structured execution.
* :class:`Permission` — ``READ_ONLY``, ``REVERSIBLE``, ``CONFIRMATION_REQUIRED``.
* :class:`ConfirmationBroker` — mints single-use grants only after a trusted
  application-side approver (e.g. a Qt dialog) returns exactly ``True``.

Phase 2 exports the bounded agent loop and the Gemini function-calling adapter:

* :class:`AgentRunner` — sequential model/tool loop with hard limits, explicit
  states, cancellation and structured results.
* :class:`AgentConfig` — 8 tool calls, 60 s per tool and 5 min per workflow by
  default, configurable through the application config.
* :class:`GeminiToolChatModel` — ``google-genai`` transport with automatic
  function calling disabled.

Importing this package has no side effects: it does not create a registry, read
files, open network connections, initialize Gemini or run tools.
"""

from engine.agent_loop import (
    AgentCancelled,
    AgentConfig,
    AgentConfigurationError,
    AgentError,
    AgentEvent,
    AgentMessage,
    AgentRunResult,
    AgentRunner,
    AgentState,
    AgentToolContext,
    CancellationToken,
    ChatModel,
    DEFAULT_SYSTEM_INSTRUCTION,
    ModelResponse,
    ToolCall,
    ToolResponse,
)
from engine.gemini_model import (
    GeminiModelError,
    GeminiToolChatModel,
    ModelConfigurationError,
    json_schema_to_gemini,
)
from engine.tool_registry import (
    ConfirmationBroker,
    ConfirmationError,
    ConfirmationGrant,
    ConfirmationRequest,
    ConfirmationSpoofError,
    Permission,
    RegisteredTool,
    ToolExecutionError,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    ToolRegistryError,
    ToolResult,
    ToolSpec,
    ToolValidationError,
    get_default_registry,
)

__all__ = [
    "AgentCancelled",
    "AgentConfig",
    "AgentConfigurationError",
    "AgentError",
    "AgentEvent",
    "AgentMessage",
    "AgentRunResult",
    "AgentRunner",
    "AgentState",
    "AgentToolContext",
    "CancellationToken",
    "ChatModel",
    "ConfirmationBroker",
    "ConfirmationError",
    "ConfirmationGrant",
    "ConfirmationRequest",
    "ConfirmationSpoofError",
    "DEFAULT_SYSTEM_INSTRUCTION",
    "GeminiModelError",
    "GeminiToolChatModel",
    "ModelConfigurationError",
    "ModelResponse",
    "Permission",
    "RegisteredTool",
    "ToolCall",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResponse",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "get_default_registry",
    "json_schema_to_gemini",
]
