"""Pydantic models for all artifact types passed between agents."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


# -- Structured Brief (Interviewer output) ------------------------------------


class UserType(BaseModel):
    type: str  # e.g. "editor", "viewer", "admin"
    primary_actions: list[str]  # What they do in the system
    access_level: str  # What they can see/modify


class DataEntity(BaseModel):
    name: str  # e.g. "Document", "User", "Comment"
    description: str  # What this represents
    relationships: list[str]  # e.g. ["owned by User", "contains Comments"]


class Assumption(BaseModel):
    assumption: str  # What was assumed
    basis: str  # Why this assumption was made
    overridable: bool  # Can the human change this?


class InterviewQuestion(BaseModel):
    dimension: str  # Which of the 5 dimensions this covers
    question: str  # The specific question
    default_assumption: str  # What we'll assume if they don't answer
    why_it_matters: str  # Why the Decomposer needs this


class StructuredBrief(BaseModel):
    system_purpose: str  # One sentence: what this system does
    user_types: list[UserType]  # Distinct interaction patterns
    core_data_entities: list[DataEntity]  # The nouns of the system
    scope_boundary: list[str]  # Explicit "NOT building" list
    technical_constraints: dict  # {platform, existing_codebase,
    #  mandated_tech, deployment}
    assumptions_made: list[Assumption]  # Gaps filled by Interviewer
    deferred_decisions: list[str]  # Things to decide later (not now)


class InterviewerOutput(BaseModel):
    brief_sufficient: bool  # True = skip questions, go to Decomposer
    structured_brief: StructuredBrief
    questions: list[InterviewQuestion]  # Empty if brief_sufficient


# -- Component Tree (Decomposer output) --------------------------------------


class Component(BaseModel):
    id: str  # e.g. "auth", "api", "database"
    name: str  # Human-readable name
    responsibility: str  # Single sentence: what this owns
    owns_data: list[str]  # Data entities this component owns
    depends_on: list[str]  # Component IDs this depends on
    exposes_to: list[str]  # Component IDs this exposes interfaces to


class ComponentTree(BaseModel):
    components: list[Component]
    data_flows: list[dict]  # [{from, to, data_description}]
    rationale: str  # Why this decomposition


# -- Interface Contracts (Contract Writer output) -----------------------------


class ErrorCase(BaseModel):
    error_type: str  # e.g. "NotFoundError"
    condition: str  # When this error occurs
    response_shape: dict  # What the error looks like
    propagation: str  # How callers should handle it


class CommunicationPattern(str, Enum):
    SYNC_CALL = "synchronous_call"
    ASYNC_EVENT = "async_event"
    HTTP_REQUEST = "http_request"
    SHARED_STATE = "shared_state"
    MESSAGE_QUEUE = "message_queue"


class StubStrategy(BaseModel):
    can_stub: bool  # Can this be mocked for parallel dev?
    stub_description: str  # What the stub returns


class InterfaceContract(BaseModel):
    boundary_id: str  # e.g. "auth-to-api"
    from_component: str  # Component ID
    to_component: str  # Component ID
    communication_pattern: CommunicationPattern
    functions: list[dict]  # [{name, params, returns, preconditions, postconditions}]
    error_cases: list[ErrorCase]
    data_schemas: dict  # Named type definitions used in this contract
    stub_strategy: StubStrategy


# -- Adversary Critique -------------------------------------------------------


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"


class Finding(BaseModel):
    severity: Severity
    affected_components: list[str]  # Component IDs
    affected_contracts: list[str]  # Boundary IDs
    finding: str  # What's wrong
    evidence: str  # Why this is a problem
    suggested_direction: str  # Hint for the Adversary Resolver


# -- Adversary Resolver Output ------------------------------------------------


class Resolution(BaseModel):
    finding_index: int  # Which finding this resolves
    action: str  # "revised" | "deferred" | "acknowledged"
    changes_made: str  # What was changed
    rationale: str  # Why this resolution


# -- Task Graph (Sequencer output) --------------------------------------------


class Complexity(str, Enum):
    TRIVIAL = "trivial"  # Boilerplate, config
    STANDARD = "standard"  # Single component, clear spec
    COMPLEX = "complex"  # Multiple interactions
    HARD = "hard"  # Novel algorithms, security-critical


class Task(BaseModel):
    id: str  # e.g. "001"
    title: str
    component: str  # Which component this implements
    description: str  # What to build
    contracts_to_satisfy: list[str]  # Boundary IDs
    files_to_create: list[str]  # New files
    files_to_modify: list[str]  # Existing files to change
    context_files: list[str]  # Files to read for context (not modify)
    acceptance_criteria: list[str]  # Machine-verifiable assertions
    not_in_scope: list[str]  # Explicit exclusions
    depends_on: list[str]  # Task IDs that must complete first
    complexity: Complexity
    estimated_tokens: Optional[int] = None  # Rough context budget needed


# -- Simulator Verdict --------------------------------------------------------


class Readiness(str, Enum):
    READY = "ready"  # Can be one-shotted
    NEEDS_REFINEMENT = "needs_refinement"
    BLOCKED = "blocked"


class TaskVerdict(BaseModel):
    task_id: str
    readiness: Readiness
    gaps: list[str]  # Specific missing information
    refinement_target: Optional[str] = None  # Which contract/component needs work


# -- Stateful Task Graph (Output Format) -------------------------------------


class TaskStatus(str, Enum):
    PENDING = "pending"            # Dependencies not yet met
    READY = "ready"                # All deps done, can be picked up
    IN_PROGRESS = "in_progress"    # Agent is working on it
    DONE = "done"                  # Implementation complete
    INVALIDATED = "invalidated"    # Was done, but upstream change broke it
    NEEDS_UPDATE = "needs_update"  # Planner flagged for revision


class DiscoveryType(str, Enum):
    MISSING_CONTRACT_FN = "missing_contract_fn"
    TASK_SPLIT_NEEDED = "task_split_needed"
    DEPENDENCY_MISSING = "dependency_missing"
    SCOPE_CHANGE = "scope_change"
    BLOCKER = "blocker"


class Discovery(BaseModel):
    found_during: str           # Task ID where this was discovered
    type: DiscoveryType
    description: str            # What was found
    affects: list[str]          # Task IDs affected
    severity: Severity          # Reuses existing Severity enum
    resolved: bool = False
    resolution: Optional[str] = None


# -- Inline Contract (compact, for prompt packets) ---------------------------


class InlineContractFn(BaseModel):
    name: str
    params: dict                # {param_name: type_string}
    returns: dict               # {field_name: type_string}
    errors: list[str]           # Error type names only


class InlineContract(BaseModel):
    boundary: str               # boundary_id
    pattern: str                # Communication pattern (short form)
    fns: list[InlineContractFn]
    error_shape: dict           # Common error response shape
    stub: str                   # Stub description for parallel dev


# -- Prompt Packet (self-contained task for coding agents) -------------------


class PromptPacket(BaseModel):
    id: str
    title: str
    v: int = 1                  # Version — increments on update
    instruction: str            # One-sentence: what to build
    component: str
    complexity: str             # Complexity value as string
    create: list[str]           # Files to create
    modify: list[str]           # Files to modify
    context: list[str]          # Files to read for context
    contracts: list[InlineContract]  # Inlined contract data
    done_when: list[str]        # Acceptance criteria
    not_this: list[str]         # Explicit exclusions
    depends: list[str]          # Task IDs this depends on
    unlocks: list[str]          # Task IDs this enables


# -- Graph Task Entry (compact, for graph.json index) ------------------------


class GraphTask(BaseModel):
    id: str
    title: str
    component: str
    complexity: str             # Complexity value as string
    status: TaskStatus
    depends: list[str]          # Task IDs
    tokens: Optional[int] = None


# -- Changelog Entry (tracks graph mutations) --------------------------------


class ChangelogEntry(BaseModel):
    v: int                      # Graph version at time of change
    action: str                 # plan_created, task_completed, etc.
    detail: str                 # Human-readable description
    affected: list[str] = []    # Task IDs affected
    ts: str                     # ISO timestamp
