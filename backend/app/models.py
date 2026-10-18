from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field

RoleType = Literal[
    "viewer",
    "member",
    "team_leader",
    "department_manager",
    "organization_head",
    "owner",
]

ScopeType = Literal["organization", "department", "team"]

AssignmentStatus = Literal["active", "suspended", "revoked"]

UserStatus = Literal["active", "suspended", "deactivated"]

WorkItemKind = Literal[
    "task",
    "incident",
    "customer_issue",
    "payment",
    "engineering",
    "compliance",
]

WorkItemStatus = Literal[
    "open",
    "in_progress",
    "awaiting_approval",
    "resolved",
    "closed",
]

WorkItemPriority = Literal["low", "normal", "high", "urgent"]


# --- User & Auth Models ---

class User(BaseModel):
    id: UUID
    email: str
    display_name: str
    status: UserStatus = "active"
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    email: str
    display_name: str
    status: UserStatus = "active"
    password: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User


class MeResponse(BaseModel):
    user: User
    positions: List["PositionResponse"] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=2, max_length=255)
    target_team_id: Optional[UUID] = None
    requested_role: Optional[Literal["viewer", "member"]] = "member"
    reason: Optional[str] = None


class RegistrationTeamOption(BaseModel):
    id: UUID
    name: str
    department_name: Optional[str] = None
    organization_name: Optional[str] = None


class RegistrationOptionsResponse(BaseModel):
    teams: List[RegistrationTeamOption] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=lambda: ["member", "viewer"])


# --- Scope Hierarchy Models ---

class Organization(BaseModel):
    id: UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class Department(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class Team(BaseModel):
    id: UUID
    department_id: UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class RoleAssignment(BaseModel):
    id: UUID
    user_id: UUID
    role: RoleType
    scope_type: ScopeType
    organization_id: Optional[UUID] = None
    department_id: Optional[UUID] = None
    team_id: Optional[UUID] = None
    status: AssignmentStatus = "active"
    granted_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class PositionResponse(BaseModel):
    assignment_id: UUID
    role: RoleType
    scope_type: ScopeType
    scope_id: UUID
    scope_name: str
    department_id: Optional[UUID] = None
    department_name: Optional[str] = None
    organization_id: Optional[UUID] = None
    organization_name: Optional[str] = None
    status: AssignmentStatus = "active"


class AuditEvent(BaseModel):
    id: UUID
    assignment_id: UUID
    user_id: UUID
    actor_id: Optional[UUID] = None
    event_type: Literal["grant", "role_change", "suspend", "restore", "revoke"]
    previous_status: Optional[str] = None
    new_status: str
    previous_role: Optional[str] = None
    new_role: str
    scope_type: ScopeType
    scope_id: UUID
    reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- Role Requests & Approvals Models ---

class RoleRequestCreate(BaseModel):
    requested_role: RoleType
    scope_type: ScopeType
    scope_id: UUID
    reason: Optional[str] = None


class RoleRequest(BaseModel):
    id: UUID
    user_id: UUID
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    requested_role: RoleType
    scope_type: ScopeType
    scope_id: UUID
    scope_name: Optional[str] = None
    reason: Optional[str] = None
    status: Literal["pending", "approved", "rejected", "cancelled"] = "pending"
    created_at: datetime
    updated_at: datetime


class RegisterResponse(BaseModel):
    user: User
    access_token: str
    token_type: str = "bearer"
    role_request: Optional[RoleRequest] = None
    requires_approval: bool = True
    message: str


class ApprovalDecisionRequest(BaseModel):
    reason: Optional[str] = None
    comment: Optional[str] = None


class ApprovalRecord(BaseModel):
    id: UUID
    request_id: UUID
    decider_id: UUID
    decider_name: Optional[str] = None
    outcome: Literal["approved", "rejected"]
    comment: Optional[str] = None
    decided_at: datetime


# --- Work Item Models ---

class WorkItem(BaseModel):
    id: UUID
    team_id: UUID
    kind: WorkItemKind
    title: str
    description: str = ""
    status: WorkItemStatus = "open"
    priority: WorkItemPriority = "normal"
    requires_approval: bool = False
    assignee_id: Optional[UUID] = None
    assignee_name: Optional[str] = None
    creator_id: UUID
    creator_name: Optional[str] = None
    team_name: Optional[str] = None
    due_date: Optional[datetime] = None
    version: int = 1
    created_at: datetime
    updated_at: datetime


class WorkItemCreate(BaseModel):
    team_id: UUID
    kind: WorkItemKind
    title: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    priority: WorkItemPriority = "normal"
    requires_approval: bool = False
    assignee_id: Optional[UUID] = None
    due_date: Optional[datetime] = None


class WorkItemUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    priority: Optional[WorkItemPriority] = None
    due_date: Optional[datetime] = None
    version: int


class WorkItemTransitionRequest(BaseModel):
    to_status: WorkItemStatus
    version: int
    reason: Optional[str] = None


class WorkItemAssignRequest(BaseModel):
    assignee_id: Optional[UUID] = None
    version: int
    reason: Optional[str] = None


class WorkItemClaimRequest(BaseModel):
    version: Optional[int] = None


class WorkItemCommentRequest(BaseModel):
    comment: str = Field(..., min_length=1)


class WorkItemListResponse(BaseModel):
    items: List[WorkItem]
    next_cursor: Optional[str] = None
    has_more: bool = False


class ActivityItem(BaseModel):
    id: UUID
    work_item_id: UUID
    actor_id: UUID
    actor_name: Optional[str] = None
    event_type: Literal["created", "comment", "status_changed", "assigned", "claimed", "released", "updated"]
    previous_status: Optional[str] = None
    new_status: Optional[str] = None
    previous_assignee_id: Optional[UUID] = None
    new_assignee_id: Optional[UUID] = None
    comment: Optional[str] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


# --- Phase 3: Outbox & Notification Models ---

OutboxStatus = Literal["pending", "processing", "completed", "dead_letter"]


class OutboxEvent(BaseModel):
    id: UUID
    event_type: str
    payload: Dict[str, Any]
    status: OutboxStatus
    attempts: int = 0
    max_attempts: int = 5
    next_attempt_at: datetime
    locked_at: Optional[datetime] = None
    locked_by: Optional[str] = None
    lease_token: Optional[UUID] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None


class NotificationItem(BaseModel):
    id: UUID
    user_id: UUID
    event_id: Optional[UUID] = None
    dedupe_key: str
    notification_type: str
    title: str
    message: str
    entity_type: Optional[str] = None
    entity_id: Optional[UUID] = None
    read_at: Optional[datetime] = None
    created_at: datetime


class NotificationUnreadCountResponse(BaseModel):
    unread_count: int


class NotificationListResponse(BaseModel):
    notifications: List[NotificationItem]
    unread_count: int
    next_cursor: Optional[str] = None
    has_more: bool = False


# --- Phase 4: Dashboard, Unified To-Do & Workspace Models ---

class DashboardWorkSummary(BaseModel):
    assigned_open_count: int
    assigned_overdue_count: int
    team_open_count: int
    team_overdue_count: int
    by_status: Dict[str, int] = Field(default_factory=dict)
    by_priority: Dict[str, int] = Field(default_factory=dict)


class DashboardApprovalsSummary(BaseModel):
    pending_role_requests_count: int
    pending_work_item_approvals_count: int
    total_pending_count: int


class DashboardResponse(BaseModel):
    user: User
    greeting: str
    positions: List[PositionResponse] = Field(default_factory=list)
    open_work_items_count: int  # Actionable/open work items across caller's accessible teams
    overdue_work_items_count: int  # Overdue work items across caller's accessible teams
    my_open_work_items_count: int  # Actionable work items directly assigned to caller
    my_overdue_work_items_count: int  # Overdue work items directly assigned to caller
    pending_approvals_count: int  # Total approval decisions requiring caller's attention
    pending_role_requests_count: int  # Role requests requiring caller's decision
    pending_work_item_approvals_count: int  # Work items awaiting caller's approval
    unread_notifications_count: int  # Unread notifications for caller
    work_summary: DashboardWorkSummary
    approvals_summary: DashboardApprovalsSummary


TodoItemType = Literal["assigned_work_item", "work_item_approval", "role_request_approval"]


class TodoItem(BaseModel):
    id: UUID
    item_type: TodoItemType
    title: str
    description: Optional[str] = None
    priority: WorkItemPriority = "normal"
    status: str
    is_overdue: bool = False
    due_date: Optional[datetime] = None
    created_at: datetime
    scope_type: Optional[str] = None
    scope_id: Optional[UUID] = None
    scope_name: Optional[str] = None
    target_id: UUID
    assignee_id: Optional[UUID] = None
    assignee_name: Optional[str] = None
    requester_id: Optional[UUID] = None
    requester_name: Optional[str] = None
    requester_email: Optional[str] = None
    requested_role: Optional[str] = None
    action_url: str


class TodoListResponse(BaseModel):
    items: List[TodoItem]
    total_count: int
    overdue_count: int
    next_cursor: Optional[str] = None
    has_more: bool = False


class TeamMemberResponse(BaseModel):
    user_id: UUID
    display_name: str
    email: str
    status: str
    role: RoleType
    role_rank: int
    scope_type: ScopeType
    assignment_id: UUID


class TeamMembersListResponse(BaseModel):
    team_id: UUID
    members: List[TeamMemberResponse]


# --- Organization Owner Dashboard Models ---

class OwnerTeamSummary(BaseModel):
    team_id: UUID
    team_name: str
    department_name: str
    total_work: int
    active_work: int
    completed_work: int
    overdue_work: int
    blocked_work: int
    attention_count: int
    members_count: int


class OwnerOrgSummary(BaseModel):
    organization_id: UUID
    organization_name: str
    total_work_items: int
    in_progress_count: int
    completed_count: int
    overdue_count: int
    blocked_count: int
    unassigned_count: int
    active_count: int
    by_status: Dict[str, int] = Field(default_factory=dict)
    by_priority: Dict[str, int] = Field(default_factory=dict)


class OwnerAttentionItem(BaseModel):
    id: UUID
    title: str
    team_name: str
    priority: WorkItemPriority
    status: WorkItemStatus
    due_date: Optional[datetime] = None
    assignee_name: Optional[str] = None
    reason: str


class OwnerDashboardResponse(BaseModel):
    organization_id: UUID
    organization_name: str
    summary: OwnerOrgSummary
    teams: List[OwnerTeamSummary]
    attention_items: List[OwnerAttentionItem]
    pending_role_requests_count: int
    pending_work_item_approvals_count: int

