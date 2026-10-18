import { useState, useEffect, useCallback, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';

// =============================================================================
// Types matching backend response schemas
// =============================================================================

interface UserProfile {
  id: string;
  email: string;
  display_name: string;
  status: string;
}

interface Position {
  assignment_id: string;
  role: string;
  status: string;
  scope_type: string;
  scope_id: string;
  scope_name?: string;
  organization_id?: string;
  organization_name?: string;
  department_id?: string;
  department_name?: string;
  team_id?: string;
  team_name?: string;
}

interface WorkItem {
  id: string;
  team_id: string;
  team_name?: string;
  kind: string;
  title: string;
  description?: string;
  status: 'open' | 'in_progress' | 'awaiting_approval' | 'resolved' | 'closed';
  priority: 'low' | 'normal' | 'high' | 'urgent';
  requires_approval: boolean;
  assignee_id?: string;
  creator_id: string;
  creator_name?: string;
  assignee_name?: string;
  due_date?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

interface ActivityEvent {
  id: string;
  work_item_id: string;
  actor_id: string;
  actor_name?: string;
  event_type: string;
  previous_status?: string;
  new_status?: string;
  previous_assignee_id?: string;
  new_assignee_id?: string;
  comment?: string;
  reason?: string;
  created_at: string;
}

interface RoleRequest {
  id: string;
  user_id: string;
  user_name?: string;
  user_email?: string;
  requested_role: string;
  scope_type: string;
  scope_id: string;
  scope_name?: string;
  reason?: string;
  status: 'pending' | 'approved' | 'rejected' | 'cancelled';
  created_at: string;
  updated_at: string;
}

interface NotificationItem {
  id: string;
  user_id: string;
  event_id?: string | null;
  dedupe_key: string;
  notification_type: string;
  title: string;
  message: string;
  entity_type?: string;
  entity_id?: string;
  read_at?: string | null;
  created_at: string;
}

interface ScopesResponse {
  organizations: { id: string; name: string; slug: string }[];
  departments: { id: string; organization_id: string; name: string; slug: string }[];
  teams: { id: string; department_id: string; name: string; slug: string }[];
}

interface DashboardWorkSummary {
  assigned_open_count: number;
  assigned_overdue_count: number;
  team_open_count: number;
  team_overdue_count: number;
  by_status: Record<string, number>;
  by_priority: Record<string, number>;
}

interface DashboardApprovalsSummary {
  pending_role_requests_count: number;
  pending_work_item_approvals_count: number;
  total_pending_count: number;
}

interface DashboardResponse {
  user: UserProfile;
  greeting: string;
  positions: Position[];
  open_work_items_count: number;
  overdue_work_items_count: number;
  my_open_work_items_count: number;
  my_overdue_work_items_count: number;
  pending_approvals_count: number;
  pending_role_requests_count: number;
  pending_work_item_approvals_count: number;
  unread_notifications_count: number;
  work_summary: DashboardWorkSummary;
  approvals_summary: DashboardApprovalsSummary;
}

interface OwnerTeamSummary {
  team_id: string;
  team_name: string;
  department_name: string;
  total_work: number;
  active_work: number;
  completed_work: number;
  overdue_work: number;
  blocked_work: number;
  attention_count: number;
  members_count: number;
}

interface OwnerOrgSummary {
  organization_id: string;
  organization_name: string;
  total_work_items: number;
  in_progress_count: number;
  completed_count: number;
  overdue_count: number;
  blocked_count: number;
  unassigned_count: number;
  active_count: number;
  by_status: Record<string, number>;
  by_priority: Record<string, number>;
}

interface OwnerAttentionItem {
  id: string;
  title: string;
  team_name: string;
  priority: 'low' | 'normal' | 'high' | 'urgent';
  status: string;
  due_date?: string | null;
  assignee_name?: string | null;
  reason: string;
}

interface OwnerDashboardResponse {
  organization_id: string;
  organization_name: string;
  summary: OwnerOrgSummary;
  teams: OwnerTeamSummary[];
  attention_items: OwnerAttentionItem[];
  pending_role_requests_count: number;
  pending_work_item_approvals_count: number;
}

interface TodoItem {
  id: string;
  item_type: 'assigned_work_item' | 'work_item_approval' | 'role_request_approval';
  title: string;
  description?: string;
  priority: 'low' | 'normal' | 'high' | 'urgent';
  status: string;
  is_overdue: boolean;
  due_date?: string;
  created_at: string;
  scope_type?: string;
  scope_id?: string;
  scope_name?: string;
  target_id: string;
  assignee_id?: string;
  assignee_name?: string;
  requester_id?: string;
  requester_name?: string;
  requester_email?: string;
  requested_role?: string;
  action_url: string;
}

interface TodoListResponse {
  items: TodoItem[];
  total_count: number;
  overdue_count: number;
  next_cursor?: string;
  has_more: boolean;
}

interface TeamMember {
  user_id: string;
  display_name: string;
  email: string;
  status: string;
  role: string;
  role_rank: number;
  scope_type: string;
  assignment_id: string;
}

interface Toast {
  id: string;
  type: 'success' | 'error' | 'info';
  message: string;
}

// =============================================================================
// Constants
// =============================================================================

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');

const DEMO_PERSONAS = [
  { name: 'Alex Vance', role: 'Owner (Acme Ops)', email: 'alex.owner@example.com' },
  { name: 'Dana Scully', role: 'Dept Mgr (Eng)', email: 'dana.eng@example.com' },
  { name: 'Rahul Patel', role: 'Team Lead (Platform)', email: 'rahul.leader@example.com' },
  { name: 'Sam Taylor', role: 'Member (Platform)', email: 'sam.member@example.com' },
  { name: 'Jordan Lee', role: 'Viewer (Design)', email: 'jordan.viewer@example.com' },
];

type TabId = 'overview' | 'work' | 'approvals' | 'todo';

// =============================================================================
// API helper
// =============================================================================

async function apiFetch<T>(
  url: string,
  token: string | null,
  options: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers as Record<string, string> || {}),
  };
  const res = await fetch(url, { ...options, headers });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    let code = 'ERROR';
    try {
      const body = await res.json();
      detail = body.message || body.detail || detail;
      code = body.code || code;
    } catch { /* ignore parse error */ }
    const err = new Error(detail) as Error & { status: number; code?: string };
    err.status = res.status;
    err.code = code;
    throw err;
  }
  return res.json() as Promise<T>;
}

// =============================================================================
// App Component
// =============================================================================

export default function App() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<TabId>('overview');
  const [token, setToken] = useState<string | null>(() => localStorage.getItem('workgrid_token'));
  const [showNotificationsDrawer, setShowNotificationsDrawer] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Work Items state
  const [workFilter, setWorkFilter] = useState<'all' | 'mine' | 'unassigned' | 'approvals' | 'overdue'>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [ownerViewMode, setOwnerViewMode] = useState<'org' | 'personal'>('org');
  const [ownerTeamFilter, setOwnerTeamFilter] = useState<string>('all');
  const [ownerWorkFilter, setOwnerWorkFilter] = useState<'all' | 'in_progress' | 'awaiting_approval' | 'overdue' | 'unassigned' | 'completed'>('all');
  const [ownerSearchQuery, setOwnerSearchQuery] = useState<string>('');
  const [selectedItemForActivity, setSelectedItemForActivity] = useState<WorkItem | null>(null);
  const [selectedItemDetail, setSelectedItemDetail] = useState<WorkItem | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showTransitionModal, setShowTransitionModal] = useState<WorkItem | null>(null);
  const [transitionStatus, setTransitionStatus] = useState<string>('in_progress');
  const [transitionReason, setTransitionReason] = useState('');

  // Comment state
  const [newComment, setNewComment] = useState('');

  // Create Work Item Form state
  const [newTitle, setNewTitle] = useState('');
  const [newKind, setNewKind] = useState('task');
  const [newPriority, setNewPriority] = useState('normal');
  const [newTeamId, setNewTeamId] = useState('');
  const [newAssigneeId, setNewAssigneeId] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newRequiresApproval, setNewRequiresApproval] = useState(false);

  // Role Request Modal state
  const [showRequestRoleModal, setShowRequestRoleModal] = useState(false);
  const [reqRole, setReqRole] = useState('member');
  const [reqScopeType, setReqScopeType] = useState('team');
  const [reqScopeId, setReqScopeId] = useState('');
  const [reqReason, setReqReason] = useState('');

  // Approval Decision Modal state
  const [decisionModal, setDecisionModal] = useState<{ req: RoleRequest; type: 'approve' | 'reject' } | null>(null);
  const [decisionComment, setDecisionComment] = useState('');

  // SSE state
  const [sseStatus, setSseStatus] = useState<'connected' | 'connecting' | 'disconnected'>('disconnected');

  // Registration and Real Account Login state
  const [authMode, setAuthMode] = useState<'personas' | 'register' | 'login'>('personas');
  const [regDisplayName, setRegDisplayName] = useState('');
  const [regEmail, setRegEmail] = useState('');
  const [regPassword, setRegPassword] = useState('');
  const [regTeamId, setRegTeamId] = useState('');
  const [regRole, setRegRole] = useState<'member' | 'viewer'>('member');
  const [regReason, setRegReason] = useState('');
  const [regSuccessData, setRegSuccessData] = useState<{
    user: UserProfile;
    role_request?: RoleRequest;
    requires_approval: boolean;
    message: string;
    access_token?: string;
  } | null>(null);

  // Email/Password login for registered accounts
  const [loginEmail, setLoginEmail] = useState('');
  const [loginPassword, setLoginPassword] = useState('');

  // =========================================================================
  // Toast system
  // =========================================================================

  const addToast = useCallback((type: Toast['type'], message: string) => {
    const id = crypto.randomUUID();
    setToasts((prev) => [...prev, { id, type, message }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 5000);
  }, []);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);


  // =========================================================================
  // Queries
  // =========================================================================

  // Current user & positions
  const meQuery = useQuery({
    queryKey: ['me', token],
    queryFn: () => apiFetch<{ user: UserProfile; positions: Position[] }>(`${API_BASE_URL}/me`, token),
    enabled: !!token,
    retry: (count, error) => {
      if ((error as Error & { status?: number }).status === 401) return false;
      return count < 2;
    },
  });

  // Handle 401 from /me to clear stale token
  useEffect(() => {
    if (meQuery.error && (meQuery.error as Error & { status?: number }).status === 401) {
      localStorage.removeItem('workgrid_token');
      setToken(null);
    }
  }, [meQuery.error]);

  // Scope tree nodes for selectors
  const scopesQuery = useQuery({
    queryKey: ['scopes', token],
    queryFn: () => apiFetch<ScopesResponse>(`${API_BASE_URL}/scopes`, token),
    enabled: !!token,
  });

  // Work items query
  const workItemsQuery = useQuery({
    queryKey: ['workItems', token, workFilter, searchQuery],
    queryFn: () => {
      const params = new URLSearchParams({ view: workFilter, limit: '50' });
      if (searchQuery.trim()) params.set('q', searchQuery.trim());
      return apiFetch<{ items: WorkItem[]; next_cursor?: string; has_more?: boolean }>(
        `${API_BASE_URL}/work-items?${params.toString()}`, token,
      );
    },
    enabled: !!token && activeTab === 'work',
  });

  // Work items summary counts
  const summaryQuery = useQuery({
    queryKey: ['workItemsSummary', token],
    queryFn: () => apiFetch<Record<string, unknown>>(`${API_BASE_URL}/work-items/summary`, token),
    enabled: !!token && activeTab === 'work',
  });

  // Activity timeline query for selected item
  const activityQuery = useQuery({
    queryKey: ['activity', token, selectedItemForActivity?.id],
    queryFn: () => apiFetch<ActivityEvent[]>(
      `${API_BASE_URL}/work-items/${selectedItemForActivity!.id}/activity`, token,
    ),
    enabled: !!token && !!selectedItemForActivity,
  });

  // Approvals inbox
  const inboxQuery = useQuery({
    queryKey: ['inbox', token],
    queryFn: () => apiFetch<RoleRequest[]>(`${API_BASE_URL}/approvals/inbox`, token),
    enabled: !!token && activeTab === 'approvals',
  });

  // My submitted role requests
  const myRequestsQuery = useQuery({
    queryKey: ['myRoleRequests', token],
    queryFn: () => apiFetch<RoleRequest[]>(`${API_BASE_URL}/me/role-requests`, token),
    enabled: !!token && activeTab === 'approvals',
  });

  // Notifications queries
  const unreadCountQuery = useQuery({
    queryKey: ['notificationsUnreadCount', token],
    queryFn: () => apiFetch<{ unread_count: number }>(`${API_BASE_URL}/notifications/unread-count`, token),
    enabled: !!token,
    refetchInterval: 15000,
  });

  const notificationsQuery = useQuery({
    queryKey: ['notifications', token],
    queryFn: () => apiFetch<{
      notifications: NotificationItem[];
      unread_count: number;
      next_cursor?: string;
      has_more?: boolean;
    }>(`${API_BASE_URL}/notifications?limit=30`, token),
    enabled: !!token && showNotificationsDrawer,
  });

  // Dashboard overview query
  const dashboardQuery = useQuery({
    queryKey: ['dashboard', token],
    queryFn: () => apiFetch<DashboardResponse>(`${API_BASE_URL}/me/dashboard`, token),
    enabled: !!token,
    refetchInterval: 30000,
  });

  // Unified To-Do queue query
  const todoQuery = useQuery({
    queryKey: ['todo', token],
    queryFn: () => apiFetch<TodoListResponse>(`${API_BASE_URL}/me/todo?limit=50`, token),
    enabled: !!token && (activeTab === 'todo' || activeTab === 'overview'),
    refetchInterval: 30000,
  });

  // Owner status derived from current user positions
  const isOwner = (meQuery.data?.positions || []).some(
    (p) => p.role === 'owner' || p.role === 'organization_head'
  );

  // Organization Owner Dashboard query
  const ownerDashboardQuery = useQuery({
    queryKey: ['ownerDashboard', token],
    queryFn: () => apiFetch<OwnerDashboardResponse>(`${API_BASE_URL}/me/owner-dashboard`, token),
    enabled: !!token && isOwner,
    refetchInterval: 30000,
  });

  // Organization-wide work items query for Owner
  const ownerWorkItemsQuery = useQuery({
    queryKey: ['ownerWorkItems', token, ownerTeamFilter, ownerWorkFilter, ownerSearchQuery],
    queryFn: () => {
      const params = new URLSearchParams();
      if (ownerTeamFilter && ownerTeamFilter !== 'all') {
        params.append('team_id', ownerTeamFilter);
      }
      if (ownerWorkFilter === 'in_progress') {
        params.append('status', 'in_progress');
      } else if (ownerWorkFilter === 'awaiting_approval') {
        params.append('status', 'awaiting_approval');
      } else if (ownerWorkFilter === 'overdue') {
        params.append('view', 'overdue');
      } else if (ownerWorkFilter === 'unassigned') {
        params.append('view', 'unassigned');
      } else if (ownerWorkFilter === 'completed') {
        params.append('status', 'resolved');
      }
      if (ownerSearchQuery.trim()) {
        params.append('q', ownerSearchQuery.trim());
      }
      params.append('limit', '50');
      return apiFetch<{ items: WorkItem[]; next_cursor?: string; has_more?: boolean }>(
        `${API_BASE_URL}/work-items?${params.toString()}`,
        token,
      );
    },
    enabled: !!token && isOwner && activeTab === 'overview',
    refetchInterval: 30000,
  });

  // Team members query for the currently selected team (only needed when create modal is open)
  const teamMembersQuery = useQuery({
    queryKey: ['teamMembers', token, newTeamId],
    queryFn: async () => {
      const data = await apiFetch<{ members: TeamMember[] }>(
        `${API_BASE_URL}/teams/${newTeamId}/members`, token,
      );
      return data.members || [];
    },
    enabled: !!token && !!newTeamId && showCreateModal,
  });

  // Auto-select first accessible team for new item and role request when scopes load
  useEffect(() => {
    const teams = scopesQuery.data?.teams || [];
    if (teams.length > 0) {
      if (!teams.some((t) => t.id === newTeamId)) {
        setNewTeamId(teams[0].id);
      }
      if (!teams.some((t) => t.id === reqScopeId)) {
        setReqScopeId(teams[0].id);
      }
    } else {
      setNewTeamId('');
      setReqScopeId('');
    }
  }, [scopesQuery.data, newTeamId, reqScopeId]);

  // =========================================================================
  // Identity Cleanup & Session Reset (WG-09)
  // =========================================================================

  const sseRef = useRef<EventSource | null>(null);

  const resetUserState = useCallback(() => {
    // 1. Close all modals, drawers, and inspect panels
    setShowNotificationsDrawer(false);
    setSelectedItemForActivity(null);
    setSelectedItemDetail(null);
    setShowCreateModal(false);
    setShowTransitionModal(null);
    setTransitionReason('');
    setShowRequestRoleModal(false);
    setDecisionModal(null);
    setDecisionComment('');
    setNewComment('');
    setNewTitle('');
    setNewDescription('');
    setNewTeamId('');
    setNewAssigneeId('');
    setReqReason('');
    setReqScopeId('');

    // 2. Terminate active SSE connection immediately
    if (sseRef.current) {
      sseRef.current.close();
      sseRef.current = null;
    }
    setSseStatus('disconnected');

    // 3. Clear all cached query data to prevent cross-persona data leakage
    queryClient.clear();
  }, [queryClient]);

  // =========================================================================
  // SSE real-time subscription (WG-03 Single-Use Ticket)
  // =========================================================================

  useEffect(() => {
    if (!token) {
      setSseStatus('disconnected');
      return;
    }

    let active = true;
    let eventSource: EventSource | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

    async function initSSE() {
      if (!active || !token) return;
      try {
        setSseStatus('connecting');
        // WG-03: Request short-lived, single-use ticket via authenticated POST
        const ticketData = await apiFetch<{ ticket: string }>(
          `${API_BASE_URL}/me/events/ticket`,
          token,
          { method: 'POST' }
        );
        if (!active) return;

        // Establish EventSource using single-use ticket instead of reusable JWT
        const sseUrl = `${API_BASE_URL}/me/events?ticket=${encodeURIComponent(ticketData.ticket)}`;
        eventSource = new EventSource(sseUrl);
        sseRef.current = eventSource;

        eventSource.addEventListener('connected', () => {
          if (active) setSseStatus('connected');
        });

        eventSource.addEventListener('work_item', () => {
          if (!active) return;
          queryClient.invalidateQueries({ queryKey: ['workItems'] });
          queryClient.invalidateQueries({ queryKey: ['workItemsSummary'] });
          queryClient.invalidateQueries({ queryKey: ['dashboard'] });
          queryClient.invalidateQueries({ queryKey: ['todo'] });
        });

        eventSource.addEventListener('notification', () => {
          if (!active) return;
          queryClient.invalidateQueries({ queryKey: ['notificationsUnreadCount'] });
          queryClient.invalidateQueries({ queryKey: ['notifications'] });
          queryClient.invalidateQueries({ queryKey: ['dashboard'] });
        });

        eventSource.addEventListener('approval', () => {
          if (!active) return;
          queryClient.invalidateQueries({ queryKey: ['inbox'] });
          queryClient.invalidateQueries({ queryKey: ['myRoleRequests'] });
          queryClient.invalidateQueries({ queryKey: ['dashboard'] });
          queryClient.invalidateQueries({ queryKey: ['todo'] });
        });

        eventSource.addEventListener('role_change', () => {
          if (!active) return;
          queryClient.invalidateQueries({ queryKey: ['me'] });
          queryClient.invalidateQueries({ queryKey: ['scopes'] });
          queryClient.invalidateQueries({ queryKey: ['dashboard'] });
          queryClient.invalidateQueries({ queryKey: ['todo'] });
          queryClient.invalidateQueries({ queryKey: ['teamMembers'] });
        });

        eventSource.addEventListener('sync', () => {
          if (!active) return;
          queryClient.invalidateQueries();
        });

        eventSource.onerror = () => {
          if (eventSource) {
            eventSource.close();
            eventSource = null;
          }
          if (active) {
            setSseStatus('connecting');
            if (reconnectTimeout) clearTimeout(reconnectTimeout);
            // Reconnect using a fresh ticket after 3s
            reconnectTimeout = setTimeout(initSSE, 3000);
          }
        };
      } catch {
        if (active) {
          setSseStatus('disconnected');
          if (reconnectTimeout) clearTimeout(reconnectTimeout);
          reconnectTimeout = setTimeout(initSSE, 5000);
        }
      }
    }

    initSSE();

    return () => {
      active = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (eventSource) {
        eventSource.close();
        eventSource = null;
      }
      sseRef.current = null;
      setSseStatus('disconnected');
    };
  }, [token, queryClient]);

  // =========================================================================
  // Mutations
  // =========================================================================

  // Login handler
  const loginMutation = useMutation({
    mutationFn: async (email: string) => {
      // WG-09: Purge previous user's selection, open modals, and cache before adopting new persona
      resetUserState();
      return apiFetch<{ access_token: string; user: UserProfile }>(`${API_BASE_URL}/auth/login`, null, {
        method: 'POST',
        body: JSON.stringify({ email, password: 'workgrid' }),
      });
    },
    onSuccess: (data) => {
      localStorage.setItem('workgrid_token', data.access_token);
      setToken(data.access_token);
      queryClient.invalidateQueries();
    },
    onError: (err: Error) => {
      addToast('error', `Authentication failed: ${err.message}`);
    },
  });

  const handleLogout = () => {
    localStorage.removeItem('workgrid_token');
    resetUserState();
    setToken(null);
  };

  // Public registration options query (teams and entry roles)
  const regOptionsQuery = useQuery({
    queryKey: ['registrationOptions'],
    queryFn: () =>
      apiFetch<{
        teams: { id: string; name: string; department_name?: string; organization_name?: string }[];
        roles: string[];
      }>(`${API_BASE_URL}/auth/registration-options`),
    enabled: !token,
  });

  // Auto-select first team when registration options load
  useEffect(() => {
    if (regOptionsQuery.data?.teams && regOptionsQuery.data.teams.length > 0 && !regTeamId) {
      setRegTeamId(regOptionsQuery.data.teams[0].id);
    }
  }, [regOptionsQuery.data, regTeamId]);

  // Real account registration mutation
  const registerMutation = useMutation({
    mutationFn: async (payload: {
      display_name: string;
      email: string;
      password: string;
      target_team_id?: string;
      requested_role: 'member' | 'viewer';
      reason?: string;
    }) => {
      const res = await fetch(`${API_BASE_URL}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Registration failed');
      }
      return res.json();
    },
    onSuccess: (data) => {
      setRegSuccessData(data);
      addToast('success', 'Account registered! Your role request is pending approval.');
    },
    onError: (err: Error) => {
      addToast('error', `Registration error: ${err.message}`);
    },
  });

  // Credential login mutation for real accounts
  const credentialLoginMutation = useMutation({
    mutationFn: async (payload: { email: string; password: string }) => {
      resetUserState();
      return apiFetch<{ access_token: string; user: UserProfile }>(`${API_BASE_URL}/auth/login`, null, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
    },
    onSuccess: (data) => {
      localStorage.setItem('workgrid_token', data.access_token);
      setToken(data.access_token);
      queryClient.invalidateQueries();
      addToast('success', `Signed in as ${data.user.display_name}`);
    },
    onError: (err: Error) => {
      addToast('error', `Authentication error: ${err.message}`);
    },
  });

  // Claim item mutation
  const claimMutation = useMutation({
    mutationFn: (itemId: string) =>
      apiFetch<WorkItem>(`${API_BASE_URL}/work-items/${itemId}/claim`, token, {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      }),
    onSuccess: (updatedItem) => {
      addToast('success', 'Work item claimed successfully.');
      setSelectedItemDetail(updatedItem);
      queryClient.invalidateQueries({ queryKey: ['workItems'] });
      queryClient.invalidateQueries({ queryKey: ['workItemsSummary'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      queryClient.invalidateQueries({ queryKey: ['todo'] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Transition item mutation
  const transitionMutation = useMutation({
    mutationFn: ({ itemId, toStatus, version, reason }: { itemId: string; toStatus: string; version: number; reason?: string }) =>
      apiFetch<WorkItem>(`${API_BASE_URL}/work-items/${itemId}/transition`, token, {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ to_status: toStatus, version, reason: reason || undefined }),
      }),
    onSuccess: (updatedItem) => {
      addToast('success', 'Status transition recorded.');
      setSelectedItemDetail(updatedItem);
      setShowTransitionModal(null);
      setTransitionReason('');
      queryClient.invalidateQueries({ queryKey: ['workItems'] });
      queryClient.invalidateQueries({ queryKey: ['workItemsSummary'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      queryClient.invalidateQueries({ queryKey: ['todo'] });
      if (selectedItemForActivity) {
        queryClient.invalidateQueries({ queryKey: ['activity', token, selectedItemForActivity.id] });
      }
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Add comment mutation
  const commentMutation = useMutation({
    mutationFn: ({ itemId, comment }: { itemId: string; comment: string }) =>
      apiFetch<ActivityEvent>(`${API_BASE_URL}/work-items/${itemId}/comments`, token, {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ comment }),
      }),
    onSuccess: () => {
      setNewComment('');
      queryClient.invalidateQueries({ queryKey: ['activity', token, selectedItemForActivity?.id] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Create work item mutation
  const createItemMutation = useMutation({
    mutationFn: () =>
      apiFetch<WorkItem>(`${API_BASE_URL}/work-items`, token, {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({
          title: newTitle,
          kind: newKind,
          priority: newPriority,
          team_id: newTeamId,
          assignee_id: newAssigneeId || undefined,
          description: newDescription || undefined,
          requires_approval: newRequiresApproval,
        }),
      }),
    onSuccess: () => {
      addToast('success', 'Work item created.');
      setShowCreateModal(false);
      setNewTitle('');
      setNewDescription('');
      setNewAssigneeId('');
      queryClient.invalidateQueries({ queryKey: ['workItems'] });
      queryClient.invalidateQueries({ queryKey: ['workItemsSummary'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      queryClient.invalidateQueries({ queryKey: ['todo'] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Submit role request mutation
  const submitRoleRequestMutation = useMutation({
    mutationFn: () =>
      apiFetch<RoleRequest>(`${API_BASE_URL}/role-requests`, token, {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({
          requested_role: reqRole,
          scope_type: reqScopeType,
          scope_id: reqScopeId,
          reason: reqReason || undefined,
        }),
      }),
    onSuccess: () => {
      setShowRequestRoleModal(false);
      setReqReason('');
      queryClient.invalidateQueries({ queryKey: ['myRoleRequests'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      queryClient.invalidateQueries({ queryKey: ['todo'] });
      addToast('success', 'Role request submitted.');
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Cancel role request mutation
  const cancelRoleRequestMutation = useMutation({
    mutationFn: (requestId: string) =>
      apiFetch<RoleRequest>(`${API_BASE_URL}/role-requests/${requestId}/cancel`, token, { method: 'POST' }),
    onSuccess: () => {
      addToast('info', 'Role request cancelled.');
      queryClient.invalidateQueries({ queryKey: ['myRoleRequests'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      queryClient.invalidateQueries({ queryKey: ['todo'] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Decision on role request mutation (Approve/Reject)
  const decisionMutation = useMutation({
    mutationFn: async () => {
      if (!decisionModal) throw new Error('No decision context');
      const { req, type } = decisionModal;
      const endpoint = `${API_BASE_URL}/role-requests/${req.id}/${type}`;
      const body = type === 'approve'
        ? { comment: decisionComment || undefined }
        : { reason: decisionComment };
      return apiFetch<Record<string, unknown>>(endpoint, token, {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify(body),
      });
    },
    onSuccess: () => {
      addToast('success', `Role request ${decisionModal?.type === 'approve' ? 'approved' : 'rejected'}.`);
      setDecisionModal(null);
      setDecisionComment('');
      queryClient.invalidateQueries({ queryKey: ['inbox'] });
      queryClient.invalidateQueries({ queryKey: ['me'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
      queryClient.invalidateQueries({ queryKey: ['todo'] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // Notification mutations
  const markReadMutation = useMutation({
    mutationFn: (id: string) =>
      apiFetch<NotificationItem>(`${API_BASE_URL}/notifications/${id}/read`, token, { method: 'POST' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notifications'] });
      queryClient.invalidateQueries({ queryKey: ['notificationsUnreadCount'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  const markAllReadMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ status: string; marked_read_count: number }>(`${API_BASE_URL}/notifications/read-all`, token, { method: 'POST' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notifications'] });
      queryClient.invalidateQueries({ queryKey: ['notificationsUnreadCount'] });
      queryClient.invalidateQueries({ queryKey: ['dashboard'] });
    },
    onError: (err: Error) => addToast('error', err.message),
  });

  // =========================================================================
  // Derived state
  // =========================================================================

  const currentUser = meQuery.data?.user;
  const positions = meQuery.data?.positions || [];
  const isAuthenticated = !!token && !!currentUser;
  const unreadCount = unreadCountQuery.data?.unread_count ?? 0;

  // =========================================================================
  // Login screen
  // =========================================================================

  if (!token) {
    return (
      <div className="login-page">
        <div className="login-split-layout">
          {/* Left: Visual Hero Pane */}
          <div className="login-hero-pane">
            <div className="login-hero-bg-layer" />
            <div className="login-hero-glow-layer" />
            <div className="login-hero-content">
              <div className="login-hero-header">
                <div className="login-logo-wrapper">
                  <img src="/logo.png" alt="WorkGrid Logo" className="login-hero-logo" />
                </div>
                <div className="login-hero-badge">
                  <span className="login-badge-pulse" />
                  <span>Next-Gen Work Coordination</span>
                </div>
              </div>

              <div className="login-hero-text">
                <h1 className="login-hero-title">
                  Welcome to <span className="login-hero-highlight">WorkGrid</span>
                </h1>
                <p className="login-hero-subtitle">
                  Bring your people, priorities, and progress together.
                </p>
              </div>

              <div className="login-hero-cards">
                <div className="login-floating-card login-float-1">
                  <div className="login-float-icon">⚡</div>
                  <div className="login-float-details">
                    <span className="login-float-title">Real-Time Synchronization</span>
                    <span className="login-float-desc">Live SSE event stream across teams</span>
                  </div>
                </div>

                <div className="login-floating-card login-float-2">
                  <div className="login-float-icon">🛡️</div>
                  <div className="login-float-details">
                    <span className="login-float-title">5-Tier Role Governance</span>
                    <span className="login-float-desc">Granular operational access control</span>
                  </div>
                </div>

                <div className="login-floating-card login-float-3">
                  <div className="login-float-icon">📈</div>
                  <div className="login-float-details">
                    <span className="login-float-title">High-Velocity Execution</span>
                    <span className="login-float-desc">Milestones, risks, and health metrics</span>
                  </div>
                </div>
              </div>

              <div className="login-hero-footer">
                <span className="login-hero-indicator" />
                <span>Enterprise SLA · ISO-aligned audit trail · Zero-leakage data isolation</span>
              </div>
            </div>
          </div>

          {/* Right: Form & Persona Selection Pane */}
          <div className="login-form-pane">
            <div className="login-container">
              <div className="login-card">
                {/* Mobile-only compact branding header */}
                <div className="login-card-mobile-brand">
                  <img src="/logo.png" alt="WorkGrid Logo" className="login-mobile-logo" />
                  <h2>Welcome to WorkGrid</h2>
                  <p>Bring your people, priorities, and progress together.</p>
                </div>

                {/* ==========================================================
                    SECTION 1: CREATE YOUR ACCOUNT (REAL REGISTRATION)
                    ========================================================== */}
                <div className="create-account-card">
                  <div className="create-account-header">
                    <div className="create-account-badge">
                      <span className="create-badge-dot" />
                      <span>Production Organization Mode</span>
                    </div>
                    <h2>Create Your Account</h2>
                    <p>
                      Register for a real WorkGrid account to coordinate workstreams, collaborate with operational teams,
                      and track organizational execution.
                    </p>
                  </div>

                  {regSuccessData ? (
                    <div className="registration-success-panel">
                      <div className="reg-success-icon">⏳</div>
                      <div className="reg-success-title">Account Created — Pending Approval</div>
                      <p className="reg-success-text">
                        Welcome, <strong>{regSuccessData.user.display_name}</strong>! Your account (<code>{regSuccessData.user.email}</code>)
                        has been registered. An onboarding request for the <strong>{regSuccessData.role_request?.requested_role || regRole}</strong> role
                        has been submitted for manager review.
                      </p>
                      <div className="reg-governance-note">
                        🔒 <strong>Multi-Tier Governance Gate:</strong> Access to protected work items and operations is restricted until an authorized team leader or manager approves your role request.
                      </div>
                      <div className="reg-success-actions">
                        {regSuccessData.access_token && (
                          <button
                            className="btn-primary"
                            onClick={() => {
                              localStorage.setItem('workgrid_token', regSuccessData.access_token!);
                              setToken(regSuccessData.access_token!);
                              queryClient.invalidateQueries();
                            }}
                          >
                            Enter Workspace (Restricted Mode)
                          </button>
                        )}
                        <button
                          className="btn-secondary"
                          onClick={() => {
                            setRegSuccessData(null);
                            setAuthMode('personas');
                          }}
                        >
                          Explore Demo Personas
                        </button>
                      </div>
                    </div>
                  ) : authMode === 'register' ? (
                    <form
                      className="registration-form"
                      onSubmit={(e) => {
                        e.preventDefault();
                        if (!regDisplayName.trim() || !regEmail.trim() || !regPassword) return;
                        registerMutation.mutate({
                          display_name: regDisplayName.trim(),
                          email: regEmail.trim(),
                          password: regPassword,
                          target_team_id: regTeamId || undefined,
                          requested_role: regRole,
                          reason: regReason.trim() || undefined,
                        });
                      }}
                    >
                      <div className="form-group">
                        <label htmlFor="reg-name">Full Name *</label>
                        <input
                          id="reg-name"
                          type="text"
                          className="form-control"
                          placeholder="e.g. Elena Rostova"
                          value={regDisplayName}
                          onChange={(e) => setRegDisplayName(e.target.value)}
                          required
                          disabled={registerMutation.isPending}
                        />
                      </div>

                      <div className="form-group">
                        <label htmlFor="reg-email">Work Email *</label>
                        <input
                          id="reg-email"
                          type="email"
                          className="form-control"
                          placeholder="elena@company.com"
                          value={regEmail}
                          onChange={(e) => setRegEmail(e.target.value)}
                          required
                          disabled={registerMutation.isPending}
                        />
                      </div>

                      <div className="form-group">
                        <label htmlFor="reg-password">Password * (min. 8 characters)</label>
                        <input
                          id="reg-password"
                          type="password"
                          className="form-control"
                          placeholder="••••••••••••"
                          value={regPassword}
                          onChange={(e) => setRegPassword(e.target.value)}
                          required
                          minLength={8}
                          disabled={registerMutation.isPending}
                        />
                      </div>

                      <div className="form-group">
                        <label htmlFor="reg-team">Onboarding Team *</label>
                        <select
                          id="reg-team"
                          className="form-control"
                          value={regTeamId}
                          onChange={(e) => setRegTeamId(e.target.value)}
                          disabled={registerMutation.isPending}
                        >
                          {regOptionsQuery.data?.teams && regOptionsQuery.data.teams.length > 0 ? (
                            regOptionsQuery.data.teams.map((t) => (
                              <option key={t.id} value={t.id}>
                                {t.name} ({t.department_name ? `${t.department_name} · ` : ''}{t.organization_name || 'Organization'})
                              </option>
                            ))
                          ) : (
                            <option value="">Acme Operations — Platform Infrastructure</option>
                          )}
                        </select>
                      </div>

                      <div className="form-group">
                        <label htmlFor="reg-role">Requested Initial Role *</label>
                        <select
                          id="reg-role"
                          className="form-control"
                          value={regRole}
                          onChange={(e) => setRegRole(e.target.value as 'member' | 'viewer')}
                          disabled={registerMutation.isPending}
                        >
                          <option value="member">Member — Operational Team Participant</option>
                          <option value="viewer">Viewer — Read-Only Observer</option>
                        </select>
                        <span className="form-help-text">
                          Role assignments require manager approval. Elevated roles (Lead, Manager) require promotion.
                        </span>
                      </div>

                      <div className="form-group">
                        <label htmlFor="reg-reason">Operational Purpose / Reason (Optional)</label>
                        <input
                          id="reg-reason"
                          type="text"
                          className="form-control"
                          placeholder="e.g. Infrastructure operations and deployment coordination"
                          value={regReason}
                          onChange={(e) => setRegReason(e.target.value)}
                          disabled={registerMutation.isPending}
                        />
                      </div>

                      {registerMutation.isError && (
                        <div className="login-error" style={{ marginBottom: '14px' }}>
                          {registerMutation.error.message}
                        </div>
                      )}

                      <div className="form-actions-row">
                        <button
                          type="submit"
                          className="btn-primary create-account-submit-btn"
                          disabled={registerMutation.isPending}
                        >
                          {registerMutation.isPending ? (
                            <>
                              <span className="loading-spinner" style={{ marginRight: '8px' }} />
                              Submitting Registration...
                            </>
                          ) : (
                            'Complete Registration & Request Access'
                          )}
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          onClick={() => setAuthMode('personas')}
                          disabled={registerMutation.isPending}
                        >
                          Cancel
                        </button>
                      </div>
                    </form>
                  ) : authMode === 'login' ? (
                    <form
                      className="registration-form"
                      onSubmit={(e) => {
                        e.preventDefault();
                        if (!loginEmail.trim() || !loginPassword) return;
                        credentialLoginMutation.mutate({
                          email: loginEmail.trim(),
                          password: loginPassword,
                        });
                      }}
                    >
                      <div className="form-group">
                        <label htmlFor="login-email-input">Work Email</label>
                        <input
                          id="login-email-input"
                          type="email"
                          className="form-control"
                          placeholder="user@example.com"
                          value={loginEmail}
                          onChange={(e) => setLoginEmail(e.target.value)}
                          required
                          disabled={credentialLoginMutation.isPending}
                        />
                      </div>
                      <div className="form-group">
                        <label htmlFor="login-password-input">Password</label>
                        <input
                          id="login-password-input"
                          type="password"
                          className="form-control"
                          placeholder="••••••••••••"
                          value={loginPassword}
                          onChange={(e) => setLoginPassword(e.target.value)}
                          required
                          disabled={credentialLoginMutation.isPending}
                        />
                      </div>

                      {credentialLoginMutation.isError && (
                        <div className="login-error" style={{ marginBottom: '14px' }}>
                          {credentialLoginMutation.error.message}
                        </div>
                      )}

                      <div className="form-actions-row">
                        <button
                          type="submit"
                          className="btn-primary create-account-submit-btn"
                          disabled={credentialLoginMutation.isPending}
                        >
                          {credentialLoginMutation.isPending ? (
                            <>
                              <span className="loading-spinner" style={{ marginRight: '8px' }} />
                              Authenticating...
                            </>
                          ) : (
                            'Sign In with Account'
                          )}
                        </button>
                        <button
                          type="button"
                          className="btn-secondary"
                          onClick={() => setAuthMode('personas')}
                          disabled={credentialLoginMutation.isPending}
                        >
                          Cancel
                        </button>
                      </div>
                    </form>
                  ) : (
                    <div className="create-account-cta-row">
                      <button
                        className="btn-primary create-account-hero-btn"
                        onClick={() => setAuthMode('register')}
                      >
                        Create Account
                      </button>
                      <button
                        className="btn-secondary sign-in-toggle-btn"
                        onClick={() => setAuthMode('login')}
                      >
                        Sign In with Email
                      </button>
                    </div>
                  )}
                </div>

                {/* ==========================================================
                    SECTION 2: DEMO PERSONAS (PRESERVED BELOW)
                    ========================================================== */}
                <div className="demo-personas-section">
                  <div className="demo-section-divider">
                    <div className="demo-divider-line" />
                    <span className="demo-divider-text">Explore the Demo — Try WorkGrid with a Demo Persona</span>
                    <div className="demo-divider-line" />
                  </div>

                  <p className="demo-section-subtext">
                    Instant access to pre-configured workspaces with sample operational data and role-specific permissions for product evaluation.
                  </p>

                  <div className="login-personas">
                    {DEMO_PERSONAS.map((p) => (
                      <button
                        key={p.email}
                        className="login-persona-btn"
                        onClick={() => loginMutation.mutate(p.email)}
                        disabled={loginMutation.isPending}
                      >
                        <span className="login-persona-name">{p.name}</span>
                        <span className="login-persona-role">{p.role}</span>
                      </button>
                    ))}
                  </div>

                  {loginMutation.isError && (
                    <div className="login-error">
                      {loginMutation.error.message}
                    </div>
                  )}

                  {loginMutation.isPending && (
                    <div style={{ textAlign: 'center', marginTop: '16px', color: 'var(--text-muted)', fontSize: '13px' }}>
                      <span className="loading-spinner" style={{ marginRight: '8px' }} />
                      Authenticating persona session...
                    </div>
                  )}
                </div>

                <div className="login-card-footer">
                  <div className="login-security-tag">
                    <span className="login-security-icon">🔒</span>
                    <span>Role-Based Governance · Real Account Mode &amp; Demo Sandbox</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Loading state while /me is resolving
  if (meQuery.isLoading) {
    return (
      <div className="login-page">
        <div style={{ textAlign: 'center', color: 'var(--text-secondary)' }}>
          <span className="loading-spinner" style={{ width: '24px', height: '24px', marginBottom: '12px', display: 'inline-block' }} />
          <div>Loading workspace...</div>
        </div>
      </div>
    );
  }

  // =========================================================================
  // Render
  // =========================================================================

  const dashData = dashboardQuery.data;
  const summaryData = summaryQuery.data as Record<string, unknown> | undefined;

  return (
    <div className="app-container">
      {/* Toast notifications */}
      {toasts.length > 0 && (
        <div className="toast-container">
          {toasts.map((t) => (
            <div key={t.id} className={`toast toast-${t.type}`}>
              <span className="toast-message">{t.message}</span>
              <button className="toast-close" onClick={() => removeToast(t.id)}>✕</button>
            </div>
          ))}
        </div>
      )}

      {/* Sidebar Navigation */}
      <aside className="sidebar" aria-label="Sidebar Navigation">
        <div className="brand-section">
          <div className="brand-logo-icon">
            <img src="/logo.png" alt="WorkGrid Logo" className="brand-logo-img" />
          </div>
          <div>
            <div className="brand-title">WorkGrid</div>
            <div className="brand-subtitle">Ops Platform</div>
          </div>
        </div>

        <nav className="nav-section">
          <div className="nav-group-title">Workspace</div>
          <button
            type="button"
            className={`nav-item ${activeTab === 'overview' ? 'active' : ''}`}
            id="nav-item-overview"
            onClick={() => setActiveTab('overview')}
            style={{ width: '100%', textAlign: 'left', background: 'none', border: 'none' }}
          >
            <span>Dashboard</span>
          </button>

          <button
            type="button"
            className={`nav-item ${activeTab === 'todo' ? 'active' : ''}`}
            id="nav-item-todo"
            onClick={() => setActiveTab('todo')}
            style={{ width: '100%', textAlign: 'left', background: 'none', border: 'none' }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span>To-Do Queue</span>
              {(todoQuery.data?.total_count ?? 0) > 0 && (
                <span
                  id="todo-count-badge"
                  style={{
                    fontSize: '10px',
                    padding: '1px 6px',
                    borderRadius: '10px',
                    backgroundColor: 'var(--accent-primary)',
                    color: '#fff',
                    fontWeight: 700,
                  }}
                >
                  {todoQuery.data!.total_count}
                </span>
              )}
            </div>
          </button>

          <button
            type="button"
            className={`nav-item ${activeTab === 'work' ? 'active' : ''}`}
            id="nav-item-work"
            onClick={() => setActiveTab('work')}
            style={{ width: '100%', textAlign: 'left', background: 'none', border: 'none' }}
          >
            <span>Work Items</span>
          </button>

          <button
            type="button"
            className={`nav-item ${activeTab === 'approvals' ? 'active' : ''}`}
            id="nav-item-approvals"
            onClick={() => setActiveTab('approvals')}
            style={{ width: '100%', textAlign: 'left', background: 'none', border: 'none' }}
          >
            <span>Approvals &amp; Roles</span>
          </button>
        </nav>

        <div className="nav-footer">
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span>Live Updates</span>
            <span className={`sse-status-badge ${sseStatus}`}>
              {sseStatus === 'connected' && '● Connected'}
              {sseStatus === 'connecting' && '◌ Connecting'}
              {sseStatus === 'disconnected' && '○ Offline'}
            </span>
          </div>
        </div>
      </aside>

      {/* Main Workspace Area */}
      <div className="main-wrapper">
        <header className="top-header">
          <div className="header-title-group">
            <img src="/logo.png" alt="WorkGrid Logo" className="dashboard-header-logo" />
            <h1>WorkGrid</h1>
          </div>

          <div className="auth-bar">
            {/* Quick Persona Selector */}
            <div className="persona-switcher">
              <span className="persona-switcher-label">Persona:</span>
              {DEMO_PERSONAS.map((p) => {
                const isActive = currentUser?.email === p.email;
                return (
                  <button
                    key={p.email}
                    className={`persona-btn ${isActive ? 'active' : ''}`}
                    onClick={() => loginMutation.mutate(p.email)}
                    title={`Log in as ${p.name} (${p.role})`}
                    disabled={loginMutation.isPending}
                  >
                    {p.name.split(' ')[0]}
                  </button>
                );
              })}
            </div>

            {/* Notification Bell Button */}
            {isAuthenticated && (
              <div style={{ position: 'relative' }}>
                <button
                  id="notification-bell-btn"
                  className="notification-bell-btn"
                  onClick={() => setShowNotificationsDrawer((prev) => !prev)}
                  title="View notifications"
                  aria-label="View notifications"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
                    <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
                  </svg>
                  {unreadCount > 0 && (
                    <span className="notification-badge" id="notification-unread-count">
                      {unreadCount > 99 ? '99+' : unreadCount}
                    </span>
                  )}
                </button>

                {showNotificationsDrawer && (
                  <>
                    <div className="notification-backdrop" onClick={() => setShowNotificationsDrawer(false)} />
                    <div className="notification-drawer" id="notification-drawer">
                      <div className="notification-header">
                        <div className="notification-header-title">
                          <span>Notifications</span>
                          {unreadCount > 0 && (
                            <span className="unread-counter-tag">
                              {unreadCount} unread
                            </span>
                          )}
                        </div>
                        <div style={{ display: 'flex', gap: '8px' }}>
                          <button
                            type="button"
                            className="btn-text-subtle"
                            onClick={() => notificationsQuery.refetch()}
                            title="Refresh notifications"
                          >
                            Refresh
                          </button>
                          {unreadCount > 0 && (
                            <button
                              type="button"
                              className="btn-text-subtle"
                              onClick={() => markAllReadMutation.mutate()}
                              disabled={markAllReadMutation.isPending}
                            >
                              Mark all read
                            </button>
                          )}
                          <button
                            type="button"
                            className="btn-text-subtle"
                            onClick={() => setShowNotificationsDrawer(false)}
                            title="Close"
                          >
                            ✕
                          </button>
                        </div>
                      </div>

                      <div className="notification-body">
                        {notificationsQuery.isLoading && (
                          <div className="notification-status-msg">Loading notifications...</div>
                        )}
                        {notificationsQuery.isError && (
                          <div className="notification-status-msg error">
                            Failed to load notifications.
                          </div>
                        )}
                        {notificationsQuery.isSuccess && notificationsQuery.data.notifications.length === 0 && (
                          <div className="notification-status-msg">
                            No notifications yet.
                          </div>
                        )}
                        {notificationsQuery.isSuccess && notificationsQuery.data.notifications.map((n) => (
                          <div
                            key={n.id}
                            className={`notification-item ${n.read_at ? 'read' : 'unread'}`}
                          >
                            <div className="notification-item-content">
                              <div className="notification-item-header">
                                <span className="notification-item-title">{n.title}</span>
                                <span className="notification-item-time">
                                  {new Date(n.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                                </span>
                              </div>
                              <p className="notification-item-message">{n.message}</p>
                            </div>
                            {!n.read_at && (
                              <button
                                type="button"
                                className="notification-mark-read-btn"
                                onClick={() => markReadMutation.mutate(n.id)}
                                title="Mark as read"
                                disabled={markReadMutation.isPending}
                              >
                                ●
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}

            {/* Current user badge */}
            {currentUser ? (
              <div className="user-badge" id="current-user-badge">
                <div>
                  <span className="user-badge-name">{currentUser.display_name}</span>
                  <div className="user-badge-role">
                    {positions.length > 0 ? `${positions[0].role} (${positions[0].scope_name || positions[0].scope_type})` : 'No active positions'}
                  </div>
                </div>
                <button className="logout-btn" onClick={handleLogout} title="Log out">
                  Logout
                </button>
              </div>
            ) : (
              <button
                className="btn-primary"
                onClick={() => loginMutation.mutate(DEMO_PERSONAS[0].email)}
              >
                Log In
              </button>
            )}
          </div>
        </header>

        <main className="content-container">
          {/* Pending Approval Restricted Access Banner for Unassigned Accounts */}
          {positions.length === 0 && (
            <div className="pending-approval-banner">
              <div className="pending-banner-icon">🔒</div>
              <div className="pending-banner-body">
                <h3>Account Pending Role Approval</h3>
                <p>
                  Welcome, <strong>{currentUser?.display_name}</strong>! Your account is active, but you do not currently hold an approved operational position.
                  In accordance with WorkGrid's role governance policy, protected operations (creating work items, status transitions, claiming work) are restricted until an authorized team leader or manager approves your onboarding role request.
                </p>
              </div>
              <button
                className="btn-secondary"
                onClick={() => setActiveTab('approvals')}
              >
                Track Role Request
              </button>
            </div>
          )}

          {/* ================================================================
              TAB 1: DASHBOARD OVERVIEW
              ================================================================ */}
          {activeTab === 'overview' && (
            <>
              {/* Not authenticated state */}
              {!isAuthenticated && (
                <div className="empty-state">
                  <div className="empty-state-icon">🔒</div>
                  <h3>Sign In Required</h3>
                  <p>Select a persona from the header to access your operational dashboard.</p>
                </div>
              )}

              {/* ============================================================
                  ORGANIZATION OWNER DASHBOARD
                  Active when user has an active owner or org_head assignment
                  ============================================================ */}
              {isAuthenticated && isOwner && ownerViewMode === 'org' && (
                <div className="owner-dashboard-container" id="owner-dashboard-container">
                  {/* Hero Header */}
                  <section className="dashboard-overview-hero" id="owner-overview-hero">
                    <div className="dashboard-hero-content">
                      <div className="dashboard-hero-title-wrap">
                        <img src="/logo.png" alt="WorkGrid Logo" className="dashboard-hero-logo" />
                        <h2>
                          {ownerDashboardQuery.data?.organization_name || 'Organization'} — Owner Dashboard
                        </h2>
                      </div>
                      <div className="dashboard-hero-subtitle">
                        <span>Organization-wide operational overview: progress, blockers, team workloads, and overdue items.</span>
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
                      <div className="owner-mode-switch">
                        <button
                          type="button"
                          className="owner-mode-btn active"
                          title="Viewing organization-wide executive data"
                        >
                          🏢 Organization View
                        </button>
                        <button
                          type="button"
                          className="owner-mode-btn"
                          onClick={() => setOwnerViewMode('personal')}
                          title="Switch to personal assigned work view"
                        >
                          👤 Personal View
                        </button>
                      </div>
                      <button
                        className="btn-primary"
                        id="btn-owner-jump-todo"
                        onClick={() => setActiveTab('todo')}
                      >
                        To-Do Queue ({(todoQuery.data?.total_count ?? 0)})
                      </button>
                      <button
                        className="refresh-btn"
                        onClick={() => {
                          ownerDashboardQuery.refetch();
                          ownerWorkItemsQuery.refetch();
                          dashboardQuery.refetch();
                        }}
                        disabled={ownerDashboardQuery.isFetching}
                        title="Refresh organization metrics"
                      >
                        {ownerDashboardQuery.isFetching ? 'Refreshing...' : 'Refresh'}
                      </button>
                    </div>
                  </section>

                  {/* Loading & Error States */}
                  {ownerDashboardQuery.isLoading && (
                    <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
                      <span className="loading-spinner" style={{ marginRight: '8px' }} />
                      Loading organization-wide metrics...
                    </div>
                  )}

                  {ownerDashboardQuery.isError && (
                    <div className="error-banner">
                      <span>Failed to load organization metrics: {ownerDashboardQuery.error?.message}</span>
                      <button onClick={() => ownerDashboardQuery.refetch()}>Retry</button>
                    </div>
                  )}

                  {ownerDashboardQuery.data && (
                    <>
                      {/* Section A: Organization Summary Stat Cards */}
                      <div className="stats-grid" id="owner-summary-metrics" style={{ marginBottom: '24px' }}>
                        <div className="stat-card" id="stat-card-owner-total">
                          <div className="stat-value">{ownerDashboardQuery.data.summary.total_work_items}</div>
                          <div className="stat-label">Total Work Items</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            Across all organization teams
                          </div>
                        </div>

                        <div className="stat-card" id="stat-card-owner-active">
                          <div className="stat-value" style={{ color: 'var(--text-primary)' }}>
                            {ownerDashboardQuery.data.summary.active_count}
                          </div>
                          <div className="stat-label">Active Work</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            {ownerDashboardQuery.data.summary.in_progress_count} in progress, {ownerDashboardQuery.data.summary.unassigned_count} unassigned
                          </div>
                        </div>

                        <div className="stat-card" id="stat-card-owner-in-progress">
                          <div className="stat-value" style={{ color: 'var(--status-info)' }}>
                            {ownerDashboardQuery.data.summary.in_progress_count}
                          </div>
                          <div className="stat-label">In Progress</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            Active development &amp; execution
                          </div>
                        </div>

                        <div className="stat-card" id="stat-card-owner-blocked">
                          <div
                            className="stat-value"
                            style={{
                              color: ownerDashboardQuery.data.summary.blocked_count > 0 ? '#D97706' : 'inherit',
                            }}
                          >
                            {ownerDashboardQuery.data.summary.blocked_count}
                          </div>
                          <div className="stat-label">Blocked (Awaiting Approval)</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            Requires managerial authorization
                          </div>
                        </div>

                        <div className="stat-card" id="stat-card-owner-overdue">
                          <div
                            className="stat-value"
                            style={{
                              color: ownerDashboardQuery.data.summary.overdue_count > 0 ? 'var(--status-error)' : 'inherit',
                            }}
                          >
                            {ownerDashboardQuery.data.summary.overdue_count}
                          </div>
                          <div className="stat-label">Overdue Work</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            Past due date without resolution
                          </div>
                        </div>

                        <div className="stat-card" id="stat-card-owner-unassigned">
                          <div className="stat-value" style={{ color: 'var(--text-secondary)' }}>
                            {ownerDashboardQuery.data.summary.unassigned_count}
                          </div>
                          <div className="stat-label">Unassigned Work</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            Open tasks awaiting ownership
                          </div>
                        </div>

                        <div className="stat-card" id="stat-card-owner-completed">
                          <div className="stat-value" style={{ color: 'var(--status-success)' }}>
                            {ownerDashboardQuery.data.summary.completed_count}
                          </div>
                          <div className="stat-label">Completed Work</div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                            Resolved or closed tasks
                          </div>
                        </div>
                      </div>

                      {/* Section D: Immediate Attention & Actionable Items */}
                      <section className="card owner-attention-section" style={{ marginBottom: '24px' }}>
                        <div className="card-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '8px' }}>
                          <div>
                            <h2>Items Requiring Attention</h2>
                            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                              Urgent, overdue tasks and blockers requiring manager or owner authorization
                            </p>
                          </div>
                          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                            {ownerDashboardQuery.data.pending_role_requests_count > 0 && (
                              <button
                                className="badge badge-awaiting_approval"
                                style={{ border: 'none', cursor: 'pointer', padding: '6px 12px', fontSize: '12px' }}
                                onClick={() => setActiveTab('approvals')}
                              >
                                {ownerDashboardQuery.data.pending_role_requests_count} Pending Role Requests →
                              </button>
                            )}
                            <span className="badge badge-urgent">
                              {ownerDashboardQuery.data.attention_items.length} Actionable Items
                            </span>
                          </div>
                        </div>

                        {ownerDashboardQuery.data.attention_items.length === 0 ? (
                          <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
                            ✓ No overdue tasks or blocked items across your organization.
                          </div>
                        ) : (
                          <div className="data-table-container" style={{ margin: 0, border: 'none' }}>
                            <table className="data-table">
                              <thead>
                                <tr>
                                  <th>Item Title</th>
                                  <th>Team</th>
                                  <th>Priority</th>
                                  <th>Status</th>
                                  <th>Due Date</th>
                                  <th>Assignee</th>
                                  <th>Attention Reason</th>
                                  <th>Action</th>
                                </tr>
                              </thead>
                              <tbody>
                                {ownerDashboardQuery.data.attention_items.map((item) => (
                                  <tr
                                    key={item.id}
                                    className="table-row-clickable"
                                    onClick={() => {
                                      apiFetch<WorkItem>(`${API_BASE_URL}/work-items/${item.id}`, token)
                                        .then((fullItem) => setSelectedItemDetail(fullItem))
                                        .catch(() => {});
                                    }}
                                  >
                                    <td style={{ fontWeight: 600 }}>{item.title}</td>
                                    <td>
                                      <span className="role-badge-pill" style={{ fontSize: '11px' }}>
                                        {item.team_name}
                                      </span>
                                    </td>
                                    <td>
                                      <span className={`badge badge-${item.priority}`}>{item.priority}</span>
                                    </td>
                                    <td>
                                      <span className={`badge badge-${item.status}`}>
                                        {item.status.replace('_', ' ')}
                                      </span>
                                    </td>
                                    <td>
                                      <span style={{ fontSize: '12px', color: item.due_date && new Date(item.due_date) < new Date() ? 'var(--status-error)' : 'inherit' }}>
                                        {item.due_date ? new Date(item.due_date).toLocaleDateString() : '—'}
                                      </span>
                                    </td>
                                    <td>
                                      <span style={{ fontSize: '12px' }}>{item.assignee_name || 'Unassigned'}</span>
                                    </td>
                                    <td>
                                      <span
                                        className="badge"
                                        style={{
                                          backgroundColor: item.reason.includes('Overdue') ? 'var(--status-error-bg)' : 'var(--status-warning-bg)',
                                          color: item.reason.includes('Overdue') ? 'var(--status-error-text)' : 'var(--status-warning-text)',
                                          borderColor: item.reason.includes('Overdue') ? 'var(--status-error-border)' : 'var(--status-warning-border)',
                                        }}
                                      >
                                        {item.reason}
                                      </span>
                                    </td>
                                    <td>
                                      <button
                                        type="button"
                                        className="btn-text-subtle"
                                        style={{ padding: '2px 8px', fontSize: '12px', textDecoration: 'underline' }}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          apiFetch<WorkItem>(`${API_BASE_URL}/work-items/${item.id}`, token)
                                            .then((fullItem) => setSelectedItemDetail(fullItem))
                                            .catch(() => {});
                                        }}
                                      >
                                        Inspect
                                      </button>
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                      </section>

                      {/* Section B: Team-Level Visibility Breakdown */}
                      <section className="card owner-teams-section" style={{ marginBottom: '24px' }}>
                        <div className="card-header">
                          <h2>Teams Overview ({ownerDashboardQuery.data.teams.length} Teams)</h2>
                          <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                            Team workloads, active execution, completion rates, and blockers by team
                          </p>
                        </div>
                        <div className="data-table-container" id="owner-teams-table" style={{ margin: 0, border: 'none' }}>
                          <table className="data-table">
                            <thead>
                              <tr>
                                <th>Team</th>
                                <th>Department</th>
                                <th>Active Members</th>
                                <th>Total Work</th>
                                <th>Active Work</th>
                                <th>Completed</th>
                                <th>Overdue</th>
                                <th>Blocked</th>
                                <th>Attention Needed</th>
                                <th>Filter</th>
                              </tr>
                            </thead>
                            <tbody>
                              {ownerDashboardQuery.data.teams.map((team) => (
                                <tr key={team.team_id}>
                                  <td style={{ fontWeight: 600 }}>{team.team_name}</td>
                                  <td>
                                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                                      {team.department_name}
                                    </span>
                                  </td>
                                  <td>
                                    <span style={{ fontSize: '12px' }}>{team.members_count} members</span>
                                  </td>
                                  <td style={{ fontWeight: 600 }}>{team.total_work}</td>
                                  <td>
                                    <span className="badge badge-in_progress">{team.active_work}</span>
                                  </td>
                                  <td>
                                    <span className="badge badge-resolved">{team.completed_work}</span>
                                  </td>
                                  <td>
                                    {team.overdue_work > 0 ? (
                                      <span className="badge badge-urgent">{team.overdue_work}</span>
                                    ) : (
                                      <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>0</span>
                                    )}
                                  </td>
                                  <td>
                                    {team.blocked_work > 0 ? (
                                      <span className="badge badge-awaiting_approval">{team.blocked_work}</span>
                                    ) : (
                                      <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>0</span>
                                    )}
                                  </td>
                                  <td>
                                    {team.attention_count > 0 ? (
                                      <span style={{ fontWeight: 700, color: '#D97706', fontSize: '13px' }}>
                                        ⚠️ {team.attention_count}
                                      </span>
                                    ) : (
                                      <span style={{ color: 'var(--status-success)', fontSize: '12px' }}>✓ On track</span>
                                    )}
                                  </td>
                                  <td>
                                    <button
                                      type="button"
                                      className={`btn-secondary ${ownerTeamFilter === team.team_id ? 'active' : ''}`}
                                      style={{ padding: '3px 8px', fontSize: '11px' }}
                                      onClick={() => {
                                        setOwnerTeamFilter((prev) => (prev === team.team_id ? 'all' : team.team_id));
                                      }}
                                    >
                                      {ownerTeamFilter === team.team_id ? 'Clear Filter' : 'Filter Work'}
                                    </button>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </section>

                      {/* Section C: Organization-Wide Work List */}
                      <section className="card owner-workstream-section" style={{ marginBottom: '24px' }}>
                        <div className="card-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
                          <div>
                            <h2>Organization Workstream Explorer</h2>
                            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                              All work items across teams in {ownerDashboardQuery.data.organization_name}
                            </p>
                          </div>
                          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
                            <select
                              className="form-control"
                              style={{ width: 'auto', minWidth: '160px', height: '34px', fontSize: '12px' }}
                              value={ownerTeamFilter}
                              onChange={(e) => setOwnerTeamFilter(e.target.value)}
                              aria-label="Filter by team"
                            >
                              <option value="all">All Teams</option>
                              {ownerDashboardQuery.data.teams.map((t) => (
                                <option key={t.team_id} value={t.team_id}>
                                  {t.team_name} ({t.department_name})
                                </option>
                              ))}
                            </select>

                            <input
                              type="text"
                              className="search-input"
                              style={{ height: '34px', minWidth: '200px', fontSize: '12px' }}
                              placeholder="Search organization items..."
                              value={ownerSearchQuery}
                              onChange={(e) => setOwnerSearchQuery(e.target.value)}
                              aria-label="Search work items"
                            />
                          </div>
                        </div>

                        {/* Status Filter Pills */}
                        <div style={{ display: 'flex', gap: '6px', padding: '12px 16px', borderBottom: '1px solid var(--border-subtle)', flexWrap: 'wrap', backgroundColor: 'var(--bg-surface-elevated)' }}>
                          {(
                            [
                              { key: 'all', label: 'All Items' },
                              { key: 'in_progress', label: 'In Progress' },
                              { key: 'awaiting_approval', label: 'Blocked (Awaiting Approval)' },
                              { key: 'overdue', label: 'Overdue' },
                              { key: 'unassigned', label: 'Unassigned' },
                              { key: 'completed', label: 'Completed' },
                            ] as const
                          ).map((f) => (
                            <button
                              key={f.key}
                              type="button"
                              className={`filter-btn ${ownerWorkFilter === f.key ? 'active' : ''}`}
                              style={{ fontSize: '12px', padding: '4px 10px' }}
                              onClick={() => setOwnerWorkFilter(f.key)}
                            >
                              {f.label}
                            </button>
                          ))}
                        </div>

                        {/* Organization-wide Table */}
                        <div className="data-table-container" id="owner-workstream-table" style={{ margin: 0, border: 'none' }}>
                          <table className="data-table">
                            <thead>
                              <tr>
                                <th>Title &amp; Description</th>
                                <th>Team</th>
                                <th>Kind</th>
                                <th>Priority</th>
                                <th>Status</th>
                                <th>Assignee</th>
                                <th>Due Date</th>
                                <th>Actions</th>
                              </tr>
                            </thead>
                            <tbody>
                              {ownerWorkItemsQuery.isLoading ? (
                                <tr>
                                  <td colSpan={8} style={{ textAlign: 'center', padding: '24px' }}>
                                    <span className="loading-spinner" style={{ marginRight: '8px' }} />
                                    Loading organization work items...
                                  </td>
                                </tr>
                              ) : (ownerWorkItemsQuery.data?.items?.length ?? 0) === 0 ? (
                                <tr>
                                  <td colSpan={8} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                                    No work items match the current team, status, or search filters.
                                  </td>
                                </tr>
                              ) : (
                                ownerWorkItemsQuery.data?.items.map((item) => (
                                  <tr
                                    key={item.id}
                                    className="table-row-clickable"
                                    onClick={() => setSelectedItemDetail(item)}
                                  >
                                    <td>
                                      <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{item.title}</div>
                                      {item.description && (
                                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px', maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                          {item.description}
                                        </div>
                                      )}
                                    </td>
                                    <td>
                                      <span className="role-badge-pill" style={{ fontSize: '11px' }}>
                                        {item.team_name || 'Team'}
                                      </span>
                                    </td>
                                    <td>
                                      <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                                        {item.kind.replace(/_/g, ' ')}
                                      </span>
                                    </td>
                                    <td>
                                      <span className={`badge badge-${item.priority}`}>{item.priority}</span>
                                    </td>
                                    <td>
                                      <span className={`badge badge-${item.status}`}>
                                        {item.status.replace(/_/g, ' ')}
                                      </span>
                                    </td>
                                    <td>
                                      {item.assignee_name ? (
                                        <span style={{ color: 'var(--text-primary)', fontSize: '12px' }}>{item.assignee_name}</span>
                                      ) : (
                                        <span style={{ color: 'var(--text-muted)', fontSize: '12px', fontStyle: 'italic' }}>Unassigned</span>
                                      )}
                                    </td>
                                    <td>
                                      <span style={{ fontSize: '12px', color: item.due_date && new Date(item.due_date) < new Date() ? 'var(--status-error)' : 'inherit' }}>
                                        {item.due_date ? new Date(item.due_date).toLocaleDateString() : '—'}
                                      </span>
                                    </td>
                                    <td>
                                      <div style={{ display: 'flex', gap: '6px' }} onClick={(e) => e.stopPropagation()}>
                                        <button
                                          className="btn-secondary"
                                          style={{ padding: '3px 8px', fontSize: '11px' }}
                                          onClick={() => setSelectedItemDetail(item)}
                                        >
                                          Details
                                        </button>
                                        {item.status === 'open' && !item.assignee_id && (
                                          <button
                                            className="btn-success"
                                            style={{ padding: '3px 8px', fontSize: '11px' }}
                                            onClick={() => claimMutation.mutate(item.id)}
                                            disabled={claimMutation.isPending}
                                          >
                                            Claim
                                          </button>
                                        )}
                                        <button
                                          className="btn-secondary"
                                          style={{ padding: '3px 8px', fontSize: '11px' }}
                                          onClick={() => {
                                            setShowTransitionModal(item);
                                            setTransitionStatus(item.status === 'open' ? 'in_progress' : 'resolved');
                                          }}
                                        >
                                          Transition
                                        </button>
                                      </div>
                                    </td>
                                  </tr>
                                ))
                              )}
                            </tbody>
                          </table>
                        </div>
                      </section>
                    </>
                  )}
                </div>
              )}

              {/* ============================================================
                  PERSONAL OPERATIONAL DASHBOARD
                  Default for non-owners, or when owner toggles to personal view
                  ============================================================ */}
              {isAuthenticated && (!isOwner || ownerViewMode === 'personal') && (
                <>
                  {dashData && (
                    <section className="dashboard-overview-hero" id="dashboard-overview-hero">
                      <div className="dashboard-hero-content">
                        <div className="dashboard-hero-title-wrap">
                          <img src="/logo.png" alt="WorkGrid Logo" className="dashboard-hero-logo" />
                          <h2>{dashData.greeting}</h2>
                        </div>
                        <div className="dashboard-hero-subtitle">
                          <span>Active Context:</span>
                          {dashData.positions.length > 0 ? (
                            dashData.positions.map((p) => (
                              <span key={p.assignment_id} className="role-badge-pill">
                                {p.role} ({p.scope_name || p.scope_type})
                              </span>
                            ))
                          ) : (
                            <span className="role-badge-pill" style={{ opacity: 0.7 }}>No active scoped assignments</span>
                          )}
                        </div>
                      </div>
                      <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexWrap: 'wrap' }}>
                        {isOwner && (
                          <button
                            type="button"
                            className="btn-secondary"
                            onClick={() => setOwnerViewMode('org')}
                            title="Switch back to Organization Owner Overview"
                          >
                            🏢 Switch to Organization View
                          </button>
                        )}
                        <button
                          className="btn-primary"
                          id="btn-jump-todo"
                          onClick={() => setActiveTab('todo')}
                        >
                          View To-Do ({(dashData.pending_approvals_count ?? 0) + (dashData.my_open_work_items_count ?? 0)})
                        </button>
                        <button
                          className="refresh-btn"
                          onClick={() => dashboardQuery.refetch()}
                          disabled={dashboardQuery.isFetching}
                          title="Refresh dashboard metrics"
                        >
                          {dashboardQuery.isFetching ? 'Refreshing...' : 'Refresh'}
                        </button>
                      </div>
                    </section>
                  )}

                  {/* Live Metric Stat Cards */}
                  {dashData && (
                    <div className="stats-grid" style={{ marginBottom: '24px' }}>
                      <div className="stat-card" id="stat-card-open-items">
                        <div className="stat-value">{dashData.open_work_items_count}</div>
                        <div className="stat-label">Accessible Open Work</div>
                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                          {dashData.my_open_work_items_count} assigned to you
                        </div>
                      </div>

                      <div className="stat-card" id="stat-card-overdue-items">
                        <div
                          className="stat-value"
                          style={{ color: dashData.overdue_work_items_count > 0 ? 'var(--status-error)' : 'inherit' }}
                        >
                          {dashData.overdue_work_items_count}
                        </div>
                        <div className="stat-label">Overdue Work Items</div>
                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                          {dashData.my_overdue_work_items_count} assigned to you
                        </div>
                      </div>

                      <div className="stat-card" id="stat-card-pending-approvals">
                        <div
                          className="stat-value"
                          style={{ color: dashData.pending_approvals_count > 0 ? 'var(--status-warning)' : 'inherit' }}
                        >
                          {dashData.pending_approvals_count}
                        </div>
                        <div className="stat-label">Pending Approvals</div>
                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                          {dashData.pending_role_requests_count} role, {dashData.pending_work_item_approvals_count} work
                        </div>
                      </div>

                      <div className="stat-card" id="stat-card-unread-notifs">
                        <div
                          className="stat-value"
                          style={{ color: dashData.unread_notifications_count > 0 ? 'var(--accent-primary)' : 'inherit' }}
                        >
                          {dashData.unread_notifications_count}
                        </div>
                        <div className="stat-label">Unread Notifications</div>
                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px' }}>
                          <button
                            type="button"
                            className="btn-text-subtle"
                            style={{ padding: 0, textDecoration: 'underline' }}
                            onClick={() => setShowNotificationsDrawer(true)}
                          >
                            Open Drawer
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Dashboard error state */}
                  {dashboardQuery.isError && (
                    <div className="error-banner">
                      <span>Failed to load dashboard data.</span>
                      <button onClick={() => dashboardQuery.refetch()}>Retry</button>
                    </div>
                  )}

                  {/* Dashboard loading */}
                  {dashboardQuery.isLoading && (
                    <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-muted)' }}>
                      <span className="loading-spinner" style={{ marginRight: '8px' }} />
                      Loading dashboard...
                    </div>
                  )}

                  {/* Work Summary Breakdown */}
                  {dashData && (
                    <div className="cards-grid">
                      <article className="card">
                        <div className="card-header">
                          <h2>Work By Status</h2>
                        </div>
                        <div className="metrics-list">
                          {Object.entries(dashData.work_summary.by_status || {}).map(([status, count]) => (
                            <div key={status} className="metric-row">
                              <span className="metric-label">
                                <span className={`badge badge-${status}`}>{status.replace('_', ' ')}</span>
                              </span>
                              <span className="metric-value">{count}</span>
                            </div>
                          ))}
                          {Object.keys(dashData.work_summary.by_status || {}).length === 0 && (
                            <div style={{ padding: '12px', color: 'var(--text-muted)', fontSize: '12px', textAlign: 'center' }}>
                              No work items in scope.
                            </div>
                          )}
                        </div>
                      </article>

                      <article className="card">
                        <div className="card-header">
                          <h2>Work By Priority</h2>
                        </div>
                        <div className="metrics-list">
                          {Object.entries(dashData.work_summary.by_priority || {}).map(([priority, count]) => (
                            <div key={priority} className="metric-row">
                              <span className="metric-label">
                                <span className={`badge badge-${priority}`}>{priority}</span>
                              </span>
                              <span className="metric-value">{count}</span>
                            </div>
                          ))}
                          {Object.keys(dashData.work_summary.by_priority || {}).length === 0 && (
                            <div style={{ padding: '12px', color: 'var(--text-muted)', fontSize: '12px', textAlign: 'center' }}>
                              No work items in scope.
                            </div>
                          )}
                        </div>
                      </article>
                    </div>
                  )}
                </>
              )}
            </>
          )}

          {/* ================================================================
              TAB 2: UNIFIED TO-DO LIST
              ================================================================ */}
          {activeTab === 'todo' && (
            <div id="unified-todo-view">
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px', flexWrap: 'wrap', gap: '12px' }}>
                <div>
                  <h2 style={{ fontSize: '18px', fontWeight: 700, color: 'var(--text-primary)' }}>Actionable To-Do</h2>
                  <p style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                    Items assigned to you and decisions requiring your action, ordered by urgency.
                  </p>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <span className="badge badge-in_progress" style={{ padding: '6px 12px' }}>
                    {todoQuery.data?.total_count ?? 0} Items
                  </span>
                  {(todoQuery.data?.overdue_count ?? 0) > 0 && (
                    <span className="badge badge-rejected" style={{ padding: '6px 12px' }}>
                      {todoQuery.data!.overdue_count} Overdue
                    </span>
                  )}
                  <button
                    className="refresh-btn"
                    onClick={() => todoQuery.refetch()}
                    disabled={todoQuery.isFetching}
                  >
                    {todoQuery.isFetching ? 'Refreshing...' : 'Refresh'}
                  </button>
                </div>
              </div>

              {todoQuery.isLoading && (
                <div style={{ padding: '40px', textAlign: 'center', color: 'var(--text-muted)' }}>
                  <span className="loading-spinner" style={{ marginRight: '8px' }} />
                  Loading to-do items...
                </div>
              )}

              {todoQuery.isError && (
                <div className="error-banner">
                  <span>Failed to load to-do queue.</span>
                  <button onClick={() => todoQuery.refetch()}>Retry</button>
                </div>
              )}

              {todoQuery.isSuccess && todoQuery.data.items.length === 0 && (
                <div className="empty-state">
                  <div className="empty-state-icon">✓</div>
                  <h3>All Caught Up</h3>
                  <p>You have no pending work items or approvals requiring action right now.</p>
                </div>
              )}

              {todoQuery.isSuccess && todoQuery.data.items.length > 0 && (
                <div className="todo-queue-container">
                  {todoQuery.data.items.map((item) => (
                    <div
                      key={item.id}
                      className={`todo-card ${item.is_overdue ? 'overdue' : ''}`}
                      id={`todo-item-${item.id}`}
                    >
                      <div className="todo-card-left">
                        <div className="todo-card-header">
                          <span className={`todo-type-badge ${item.item_type === 'assigned_work_item' ? 'assigned' : item.item_type === 'work_item_approval' ? 'approval' : 'role_request'}`}>
                            {item.item_type === 'assigned_work_item' && 'Assigned Work'}
                            {item.item_type === 'work_item_approval' && 'Work Approval'}
                            {item.item_type === 'role_request_approval' && 'Role Request'}
                          </span>
                          {item.is_overdue && <span className="todo-overdue-pill">Overdue</span>}
                          <span className={`badge badge-${item.priority}`}>{item.priority}</span>
                          <span className={`badge badge-${item.status}`}>{item.status.replace(/_/g, ' ')}</span>
                        </div>

                        <div className="todo-title">{item.title}</div>
                        {item.description && <div className="todo-description">{item.description}</div>}

                        <div className="todo-metadata-row">
                          {item.scope_name && (
                            <div className="todo-meta-item">
                              <span>Scope:</span>
                              <strong>{item.scope_name}</strong>
                            </div>
                          )}
                          {item.due_date && (
                            <div className="todo-meta-item" style={{ color: item.is_overdue ? 'var(--status-error)' : 'inherit' }}>
                              <span>Due:</span>
                              <strong>{new Date(item.due_date).toLocaleDateString()}</strong>
                            </div>
                          )}
                          <div className="todo-meta-item">
                            <span>Created:</span>
                            <span>{new Date(item.created_at).toLocaleDateString()}</span>
                          </div>
                          {item.assignee_name && (
                            <div className="todo-meta-item">
                              <span>Assignee:</span>
                              <span>{item.assignee_name}</span>
                            </div>
                          )}
                          {item.requester_name && (
                            <div className="todo-meta-item">
                              <span>Requester:</span>
                              <span>{item.requester_name}</span>
                            </div>
                          )}
                        </div>
                      </div>

                      <div className="todo-card-actions">
                        {item.item_type === 'assigned_work_item' && (
                          <button
                            className="btn-primary"
                            style={{ padding: '6px 12px', fontSize: '12px' }}
                            onClick={() => {
                              setActiveTab('work');
                              workItemsQuery.refetch();
                            }}
                          >
                            View in Work Items
                          </button>
                        )}

                        {item.item_type === 'work_item_approval' && (
                          <button
                            className="btn-primary"
                            style={{ padding: '6px 12px', fontSize: '12px' }}
                            onClick={() => {
                              setActiveTab('work');
                              workItemsQuery.refetch();
                            }}
                          >
                            Review &amp; Decide
                          </button>
                        )}

                        {item.item_type === 'role_request_approval' && (
                          <>
                            <button
                              className="btn-success"
                              style={{ padding: '6px 12px', fontSize: '12px' }}
                              onClick={() => {
                                setDecisionModal({
                                  req: {
                                    id: item.target_id,
                                    user_id: item.requester_id || '',
                                    user_name: item.requester_name,
                                    user_email: item.requester_email,
                                    requested_role: item.requested_role || '',
                                    scope_type: item.scope_type || 'team',
                                    scope_id: item.scope_id || '',
                                    scope_name: item.scope_name,
                                    reason: item.description,
                                    status: 'pending',
                                    created_at: item.created_at,
                                    updated_at: item.created_at,
                                  },
                                  type: 'approve',
                                });
                              }}
                            >
                              Approve
                            </button>
                            <button
                              className="btn-danger"
                              style={{ padding: '6px 12px', fontSize: '12px' }}
                              onClick={() => {
                                setDecisionModal({
                                  req: {
                                    id: item.target_id,
                                    user_id: item.requester_id || '',
                                    user_name: item.requester_name,
                                    user_email: item.requester_email,
                                    requested_role: item.requested_role || '',
                                    scope_type: item.scope_type || 'team',
                                    scope_id: item.scope_id || '',
                                    scope_name: item.scope_name,
                                    reason: item.description,
                                    status: 'pending',
                                    created_at: item.created_at,
                                    updated_at: item.created_at,
                                  },
                                  type: 'reject',
                                });
                              }}
                            >
                              Reject
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ================================================================
              TAB 3: WORK ITEMS
              ================================================================ */}
          {activeTab === 'work' && (
            <div>
              {/* Work item detail panel */}
              {selectedItemDetail && (
                <div className="detail-panel" id="work-item-detail">
                  <div className="detail-panel-header">
                    <div>
                      <h3 style={{ fontSize: '16px', fontWeight: 600, color: 'var(--text-primary)' }}>{selectedItemDetail.title}</h3>
                      <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
                        ID: {selectedItemDetail.id}
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                      <span className={`badge badge-${selectedItemDetail.status}`}>{selectedItemDetail.status.replace(/_/g, ' ')}</span>
                      <span className={`badge badge-${selectedItemDetail.priority}`}>{selectedItemDetail.priority}</span>
                      <button className="btn-secondary" onClick={() => setSelectedItemDetail(null)}>Close</button>
                    </div>
                  </div>
                  <div className="detail-panel-body">
                    {selectedItemDetail.description && (
                      <div className="detail-field">
                        <div className="detail-field-label">Description</div>
                        <div className="detail-field-value">{selectedItemDetail.description}</div>
                      </div>
                    )}
                    <div className="detail-meta-grid">
                      <div className="detail-field">
                        <div className="detail-field-label">Kind</div>
                        <div className="detail-field-value">{selectedItemDetail.kind.replace(/_/g, ' ')}</div>
                      </div>
                      <div className="detail-field">
                        <div className="detail-field-label">Assignee</div>
                        <div className="detail-field-value">{selectedItemDetail.assignee_name || 'Unassigned'}</div>
                      </div>
                      <div className="detail-field">
                        <div className="detail-field-label">Creator</div>
                        <div className="detail-field-value">{selectedItemDetail.creator_name || '—'}</div>
                      </div>
                      <div className="detail-field">
                        <div className="detail-field-label">Version</div>
                        <div className="detail-field-value" style={{ fontFamily: 'var(--font-mono)' }}>v{selectedItemDetail.version}</div>
                      </div>
                      <div className="detail-field">
                        <div className="detail-field-label">Created</div>
                        <div className="detail-field-value">{new Date(selectedItemDetail.created_at).toLocaleString()}</div>
                      </div>
                      <div className="detail-field">
                        <div className="detail-field-label">Updated</div>
                        <div className="detail-field-value">{new Date(selectedItemDetail.updated_at).toLocaleString()}</div>
                      </div>
                      {selectedItemDetail.due_date && (
                        <div className="detail-field">
                          <div className="detail-field-label">Due Date</div>
                          <div className="detail-field-value">{new Date(selectedItemDetail.due_date).toLocaleDateString()}</div>
                        </div>
                      )}
                      <div className="detail-field">
                        <div className="detail-field-label">Requires Approval</div>
                        <div className="detail-field-value">{selectedItemDetail.requires_approval ? 'Yes' : 'No'}</div>
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', marginTop: '16px' }}>
                      {selectedItemDetail.status === 'open' && !selectedItemDetail.assignee_id && (
                        <button className="btn-success" onClick={() => claimMutation.mutate(selectedItemDetail.id)} disabled={claimMutation.isPending}>
                          Claim
                        </button>
                      )}
                      <button className="btn-secondary" onClick={() => {
                        setShowTransitionModal(selectedItemDetail);
                        setTransitionStatus(selectedItemDetail.status === 'open' ? 'in_progress' : 'resolved');
                      }}>
                        Transition
                      </button>
                      <button className="btn-secondary" onClick={() => setSelectedItemForActivity(selectedItemDetail)}>
                        Activity &amp; Comments
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {/* Summary stats */}
              <div className="stats-grid">
                <div className="stat-card">
                  <div className="stat-value">{(summaryData?.total as number) ?? 0}</div>
                  <div className="stat-label">Total Accessible</div>
                </div>
                <div className="stat-card">
                  <div className="stat-value">{((summaryData?.by_status as Record<string, number>)?.open) ?? 0}</div>
                  <div className="stat-label">Open</div>
                </div>
                <div className="stat-card">
                  <div className="stat-value">{((summaryData?.by_status as Record<string, number>)?.in_progress) ?? 0}</div>
                  <div className="stat-label">In Progress</div>
                </div>
                <div className="stat-card">
                  <div className="stat-value">{((summaryData?.by_status as Record<string, number>)?.awaiting_approval) ?? 0}</div>
                  <div className="stat-label">Awaiting Approval</div>
                </div>
                <div className="stat-card">
                  <div className="stat-value">
                    {(((summaryData?.by_status as Record<string, number>)?.resolved) ?? 0) +
                     (((summaryData?.by_status as Record<string, number>)?.closed) ?? 0)}
                  </div>
                  <div className="stat-label">Resolved / Closed</div>
                </div>
                <div className="stat-card">
                  <div className="stat-value" style={{ color: ((summaryData?.overdue as number) ?? 0) > 0 ? 'var(--status-error)' : 'inherit' }}>
                    {(summaryData?.overdue as number) ?? 0}
                  </div>
                  <div className="stat-label">Overdue</div>
                </div>
              </div>

              {/* Action bar & Filters */}
              <div className="action-bar">
                <div className="filter-group">
                  {(['all', 'mine', 'unassigned', 'approvals', 'overdue'] as const).map((v) => (
                    <button
                      key={v}
                      className={`filter-btn ${workFilter === v ? 'active' : ''}`}
                      onClick={() => setWorkFilter(v)}
                    >
                      {v.charAt(0).toUpperCase() + v.slice(1)}
                    </button>
                  ))}
                </div>

                <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
                  <input
                    type="text"
                    className="search-input"
                    placeholder="Search work items..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    aria-label="Search work items"
                  />
                  <button
                    id="btn-create-work-item"
                    className="btn-primary"
                    onClick={() => {
                      if (positions.length === 0) {
                        addToast('error', 'Role approval required: Unassigned accounts cannot create work items until approved by a manager.');
                        return;
                      }
                      setShowCreateModal(true);
                    }}
                    title={positions.length === 0 ? "Role approval required to create work items" : undefined}
                  >
                    + New Work Item
                  </button>
                </div>
              </div>

              {/* Work Items error */}
              {workItemsQuery.isError && (
                <div className="error-banner">
                  <span>Failed to load work items.</span>
                  <button onClick={() => workItemsQuery.refetch()}>Retry</button>
                </div>
              )}

              {/* Work Items Table */}
              <div className="data-table-container">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Title &amp; Description</th>
                      <th>Kind</th>
                      <th>Priority</th>
                      <th>Status</th>
                      <th>Assignee</th>
                      <th>Ver</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {workItemsQuery.isLoading ? (
                      <tr>
                        <td colSpan={7} style={{ textAlign: 'center', padding: '24px' }}>
                          <span className="loading-spinner" style={{ marginRight: '8px' }} />
                          Loading accessible items...
                        </td>
                      </tr>
                    ) : (workItemsQuery.data?.items?.length ?? 0) === 0 ? (
                      <tr>
                        <td colSpan={7} style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                          No work items found matching this view or scope.
                        </td>
                      </tr>
                    ) : (
                      workItemsQuery.data?.items.map((item) => (
                        <tr key={item.id} className="table-row-clickable" onClick={() => setSelectedItemDetail(item)}>
                          <td>
                            <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{item.title}</div>
                            {item.description && (
                              <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px', maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {item.description}
                              </div>
                            )}
                          </td>
                          <td>
                            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                              {item.kind.replace(/_/g, ' ')}
                            </span>
                          </td>
                          <td>
                            <span className={`badge badge-${item.priority}`}>{item.priority}</span>
                          </td>
                          <td>
                            <span className={`badge badge-${item.status}`}>{item.status.replace(/_/g, ' ')}</span>
                          </td>
                          <td>
                            {item.assignee_name ? (
                              <span style={{ color: 'var(--text-primary)', fontSize: '12px' }}>{item.assignee_name}</span>
                            ) : (
                              <span style={{ color: 'var(--text-muted)', fontSize: '12px', fontStyle: 'italic' }}>Unassigned</span>
                            )}
                          </td>
                          <td>
                            <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px' }}>v{item.version}</span>
                          </td>
                          <td>
                            <div style={{ display: 'flex', gap: '6px' }} onClick={(e) => e.stopPropagation()}>
                              {item.status === 'open' && !item.assignee_id && (
                                <button
                                  className="btn-success"
                                  onClick={() => claimMutation.mutate(item.id)}
                                  disabled={claimMutation.isPending}
                                >
                                  Claim
                                </button>
                              )}
                              <button
                                className="btn-secondary"
                                onClick={() => {
                                  setShowTransitionModal(item);
                                  setTransitionStatus(item.status === 'open' ? 'in_progress' : 'resolved');
                                }}
                              >
                                Transition
                              </button>
                              <button
                                className="btn-secondary"
                                onClick={() => setSelectedItemForActivity(item)}
                              >
                                Activity
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* ================================================================
              TAB 4: APPROVALS & ROLES
              ================================================================ */}
          {activeTab === 'approvals' && (
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                <div>
                  <h2 style={{ fontSize: '18px', fontWeight: 600 }}>Approvals &amp; Role Governance</h2>
                  <p style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    Role request approvals, active positions, and scope assignments.
                  </p>
                </div>
                <button
                  className="btn-primary"
                  onClick={() => setShowRequestRoleModal(true)}
                >
                  + Request Role
                </button>
              </div>

              {/* Inbox error */}
              {inboxQuery.isError && (
                <div className="error-banner">
                  <span>Failed to load approvals inbox.</span>
                  <button onClick={() => inboxQuery.refetch()}>Retry</button>
                </div>
              )}

              {/* Pending Inbox Card */}
              <div className="card" style={{ marginBottom: '24px' }}>
                <div className="card-header">
                  <h2>Pending Approvals Inbox</h2>
                  <span className="badge badge-awaiting_approval">{inboxQuery.data?.length ?? 0} Pending</span>
                </div>
                <div className="data-table-container" style={{ margin: 0, border: 'none' }}>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Applicant</th>
                        <th>Requested Role</th>
                        <th>Target Scope</th>
                        <th>Reason</th>
                        <th>Submitted</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {inboxQuery.isLoading ? (
                        <tr>
                          <td colSpan={6} style={{ textAlign: 'center', padding: '20px' }}>
                            <span className="loading-spinner" style={{ marginRight: '8px' }} />
                            Loading inbox...
                          </td>
                        </tr>
                      ) : (inboxQuery.data?.length ?? 0) === 0 ? (
                        <tr>
                          <td colSpan={6} style={{ textAlign: 'center', padding: '20px', color: 'var(--text-muted)' }}>
                            No pending requests requiring your approval.
                          </td>
                        </tr>
                      ) : (
                        inboxQuery.data?.map((req) => (
                          <tr key={req.id}>
                            <td>
                              <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{req.user_name}</div>
                              <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{req.user_email}</div>
                            </td>
                            <td>
                              <span className="badge badge-normal">{req.requested_role}</span>
                            </td>
                            <td>
                              <div style={{ fontSize: '12px' }}>{req.scope_name || req.scope_type}</div>
                              <div style={{ fontSize: '10px', color: 'var(--text-muted)' }}>({req.scope_type})</div>
                            </td>
                            <td>
                              <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{req.reason || '—'}</span>
                            </td>
                            <td>
                              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                                {new Date(req.created_at).toLocaleDateString()}
                              </span>
                            </td>
                            <td>
                              <div style={{ display: 'flex', gap: '6px' }}>
                                <button
                                  className="btn-success"
                                  onClick={() => setDecisionModal({ req, type: 'approve' })}
                                >
                                  Approve
                                </button>
                                <button
                                  className="btn-danger"
                                  onClick={() => setDecisionModal({ req, type: 'reject' })}
                                >
                                  Reject
                                </button>
                              </div>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* My Requests & Active Positions Grid */}
              <div className="cards-grid">
                {/* My Submitted Requests */}
                <article className="card">
                  <div className="card-header">
                    <h2>My Role Requests</h2>
                  </div>
                  <div className="metrics-list" style={{ maxHeight: '300px', overflowY: 'auto' }}>
                    {myRequestsQuery.isLoading ? (
                      <div style={{ padding: '16px', textAlign: 'center' }}>
                        <span className="loading-spinner" />
                      </div>
                    ) : (myRequestsQuery.data?.length ?? 0) === 0 ? (
                      <div style={{ padding: '16px', color: 'var(--text-muted)', fontSize: '12px', textAlign: 'center' }}>
                        You have not submitted any role requests yet.
                      </div>
                    ) : (
                      myRequestsQuery.data?.map((r) => (
                        <div key={r.id} className="metric-row" style={{ alignItems: 'center' }}>
                          <div>
                            <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{r.requested_role}</span>
                            <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                              Scope: {r.scope_name || r.scope_type}
                            </div>
                          </div>
                          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                            <span className={`badge badge-${r.status}`}>{r.status}</span>
                            {r.status === 'pending' && (
                              <button
                                className="btn-danger"
                                style={{ padding: '2px 6px', fontSize: '10px' }}
                                onClick={() => cancelRoleRequestMutation.mutate(r.id)}
                                disabled={cancelRoleRequestMutation.isPending}
                              >
                                Cancel
                              </button>
                            )}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </article>

                {/* My Active Positions */}
                <article className="card">
                  <div className="card-header">
                    <h2>My Active Positions</h2>
                  </div>
                  <div className="metrics-list" style={{ maxHeight: '300px', overflowY: 'auto' }}>
                    {positions.length === 0 ? (
                      <div style={{ padding: '16px', color: 'var(--text-muted)', fontSize: '12px', textAlign: 'center' }}>
                        No active scoped positions assigned.
                      </div>
                    ) : (
                      positions.map((p) => (
                        <div key={p.assignment_id} className="metric-row">
                          <div>
                            <span style={{ fontWeight: 600, color: 'var(--accent-primary)' }}>{p.role}</span>
                            <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                              {p.team_name ? `Team: ${p.team_name}` : p.department_name ? `Dept: ${p.department_name}` : `Org: ${p.organization_name}`}
                            </div>
                          </div>
                          <span className={`badge badge-${p.status}`}>{p.status}</span>
                        </div>
                      ))
                    )}
                  </div>
                </article>
              </div>
            </div>
          )}
        </main>
      </div>

      {/* =================================================================
          MODAL: Create Work Item
          ================================================================= */}
      {showCreateModal && (
        <div className="modal-overlay" onClick={() => setShowCreateModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Create Work Item</h3>
              <button className="modal-close-btn" onClick={() => setShowCreateModal(false)} aria-label="Close">✕</button>
            </div>
            <div className="modal-body">
              <div className="form-group">
                <label htmlFor="create-title">Title *</label>
                <input
                  id="create-title"
                  type="text"
                  className="form-control"
                  placeholder="Operational title"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                />
              </div>

              <div className="form-group">
                <label htmlFor="create-team">Target Team *</label>
                <select
                  id="create-team"
                  className="form-control"
                  value={newTeamId}
                  onChange={(e) => {
                    setNewTeamId(e.target.value);
                    setNewAssigneeId('');
                  }}
                >
                  {scopesQuery.data?.teams.map((t) => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="create-assignee">Assignee (Optional)</label>
                <select
                  id="create-assignee"
                  className="form-control"
                  value={newAssigneeId}
                  onChange={(e) => setNewAssigneeId(e.target.value)}
                >
                  <option value="">Unassigned (Open for Claim)</option>
                  {teamMembersQuery.data?.map((m) => (
                    <option key={m.user_id} value={m.user_id}>
                      {m.display_name} ({m.role})
                    </option>
                  ))}
                </select>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div className="form-group">
                  <label htmlFor="create-kind">Kind</label>
                  <select id="create-kind" className="form-control" value={newKind} onChange={(e) => setNewKind(e.target.value)}>
                    <option value="task">Task</option>
                    <option value="incident">Incident</option>
                    <option value="customer_issue">Customer Issue</option>
                    <option value="payment">Payment</option>
                    <option value="engineering">Engineering</option>
                    <option value="compliance">Compliance</option>
                  </select>
                </div>
                <div className="form-group">
                  <label htmlFor="create-priority">Priority</label>
                  <select id="create-priority" className="form-control" value={newPriority} onChange={(e) => setNewPriority(e.target.value)}>
                    <option value="low">Low</option>
                    <option value="normal">Normal</option>
                    <option value="high">High</option>
                    <option value="urgent">Urgent</option>
                  </select>
                </div>
              </div>

              <div className="form-group">
                <label htmlFor="create-description">Description</label>
                <textarea
                  id="create-description"
                  className="form-control"
                  rows={3}
                  placeholder="Task details and scope"
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                />
              </div>

              <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <input
                  type="checkbox"
                  id="chk-req-approval"
                  checked={newRequiresApproval}
                  onChange={(e) => setNewRequiresApproval(e.target.checked)}
                />
                <label htmlFor="chk-req-approval" style={{ margin: 0, textTransform: 'none', cursor: 'pointer' }}>
                  Requires formal approval before resolution
                </label>
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setShowCreateModal(false)}>Cancel</button>
              <button
                className="btn-primary"
                onClick={() => createItemMutation.mutate()}
                disabled={!newTitle.trim() || createItemMutation.isPending}
              >
                {createItemMutation.isPending ? 'Creating...' : 'Create Item'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* =================================================================
          MODAL: Transition Work Item
          ================================================================= */}
      {showTransitionModal && (
        <div className="modal-overlay" onClick={() => setShowTransitionModal(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Transition: {showTransitionModal.title}</h3>
              <button className="modal-close-btn" onClick={() => setShowTransitionModal(null)} aria-label="Close">✕</button>
            </div>
            <div className="modal-body">
              <div className="form-group">
                <label>Current Status</label>
                <div style={{ marginBottom: '8px' }}>
                  <span className={`badge badge-${showTransitionModal.status}`}>{showTransitionModal.status.replace(/_/g, ' ')}</span>
                  <span style={{ marginLeft: '8px', fontSize: '11px', color: 'var(--text-muted)' }}>
                    Version {showTransitionModal.version}
                  </span>
                </div>
              </div>

              <div className="form-group">
                <label htmlFor="transition-status">Target Status *</label>
                <select
                  id="transition-status"
                  className="form-control"
                  value={transitionStatus}
                  onChange={(e) => setTransitionStatus(e.target.value)}
                >
                  <option value="open">Open (Release ownership)</option>
                  <option value="in_progress">In Progress</option>
                  <option value="awaiting_approval">Awaiting Approval</option>
                  <option value="resolved">Resolved</option>
                  <option value="closed">Closed</option>
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="transition-reason">Reason / Audit Note</label>
                <input
                  id="transition-reason"
                  type="text"
                  className="form-control"
                  placeholder="Required if closing or rejecting"
                  value={transitionReason}
                  onChange={(e) => setTransitionReason(e.target.value)}
                />
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setShowTransitionModal(null)}>Cancel</button>
              <button
                className="btn-primary"
                onClick={() =>
                  transitionMutation.mutate({
                    itemId: showTransitionModal.id,
                    toStatus: transitionStatus,
                    version: showTransitionModal.version,
                    reason: transitionReason,
                  })
                }
                disabled={transitionMutation.isPending}
              >
                {transitionMutation.isPending ? 'Saving...' : 'Confirm Transition'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* =================================================================
          MODAL: Activity Timeline & Comments
          ================================================================= */}
      {selectedItemForActivity && (
        <div className="modal-overlay" onClick={() => setSelectedItemForActivity(null)}>
          <div className="modal-content" style={{ maxWidth: '640px' }} onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h3>Activity: {selectedItemForActivity.title}</h3>
                <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
                  Audit trail (ID: {selectedItemForActivity.id})
                </div>
              </div>
              <button className="modal-close-btn" onClick={() => setSelectedItemForActivity(null)} aria-label="Close">✕</button>
            </div>
            <div className="modal-body">
              {/* Comment submission form */}
              <div style={{ display: 'flex', gap: '8px', marginBottom: '16px' }}>
                <input
                  type="text"
                  className="form-control"
                  placeholder="Add a comment..."
                  value={newComment}
                  onChange={(e) => setNewComment(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && newComment.trim()) {
                      commentMutation.mutate({ itemId: selectedItemForActivity.id, comment: newComment.trim() });
                    }
                  }}
                  aria-label="Add comment"
                />
                <button
                  className="btn-primary"
                  onClick={() => commentMutation.mutate({ itemId: selectedItemForActivity.id, comment: newComment.trim() })}
                  disabled={!newComment.trim() || commentMutation.isPending}
                >
                  Comment
                </button>
              </div>

              {/* Timeline list */}
              <div className="activity-timeline">
                {activityQuery.isLoading ? (
                  <div style={{ padding: '16px', color: 'var(--text-muted)' }}>
                    <span className="loading-spinner" style={{ marginRight: '8px' }} />
                    Loading timeline...
                  </div>
                ) : activityQuery.isError ? (
                  <div style={{ padding: '16px', color: 'var(--status-error)' }}>
                    Failed to load activity.
                  </div>
                ) : (activityQuery.data?.length ?? 0) === 0 ? (
                  <div style={{ padding: '16px', color: 'var(--text-muted)' }}>No recorded events.</div>
                ) : (
                  activityQuery.data?.map((act) => (
                    <div key={act.id} className="timeline-item">
                      <div style={{ flex: 1 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span className="timeline-event-name">
                            {act.actor_name || 'System'} — {act.event_type.replace(/_/g, ' ')}
                          </span>
                          <span className="timeline-meta">{new Date(act.created_at).toLocaleTimeString()}</span>
                        </div>
                        {act.new_status && (
                          <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                            Status: {act.previous_status || 'none'} → <strong>{act.new_status}</strong>
                          </div>
                        )}
                        {act.comment && <div className="timeline-comment">&quot;{act.comment}&quot;</div>}
                        {act.reason && <div className="timeline-comment" style={{ borderColor: 'var(--border-strong)' }}>Reason: {act.reason}</div>}
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setSelectedItemForActivity(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {/* =================================================================
          MODAL: Submit Role Request
          ================================================================= */}
      {showRequestRoleModal && (
        <div className="modal-overlay" onClick={() => setShowRequestRoleModal(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Submit Role Request</h3>
              <button className="modal-close-btn" onClick={() => setShowRequestRoleModal(false)} aria-label="Close">✕</button>
            </div>
            <div className="modal-body">
              <div className="form-group">
                <label htmlFor="req-role">Requested Role *</label>
                <select id="req-role" className="form-control" value={reqRole} onChange={(e) => setReqRole(e.target.value)}>
                  <option value="viewer">Viewer</option>
                  <option value="member">Member</option>
                  <option value="team_leader">Team Leader</option>
                  <option value="department_manager">Department Manager</option>
                  <option value="organization_head">Organization Head</option>
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="req-scope-type">Scope Type *</label>
                <select
                  id="req-scope-type"
                  className="form-control"
                  value={reqScopeType}
                  onChange={(e) => {
                    const next = e.target.value;
                    setReqScopeType(next);
                    if (next === 'team' && scopesQuery.data?.teams?.[0]) {
                      setReqScopeId(scopesQuery.data.teams[0].id);
                    } else if (next === 'department' && scopesQuery.data?.departments?.[0]) {
                      setReqScopeId(scopesQuery.data.departments[0].id);
                    } else if (next === 'organization' && scopesQuery.data?.organizations?.[0]) {
                      setReqScopeId(scopesQuery.data.organizations[0].id);
                    }
                  }}
                >
                  <option value="team">Team Scope</option>
                  <option value="department">Department Scope</option>
                  <option value="organization">Organization Scope</option>
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="req-scope-id">Target Scope *</label>
                <select id="req-scope-id" className="form-control" value={reqScopeId} onChange={(e) => setReqScopeId(e.target.value)}>
                  {reqScopeType === 'team' && scopesQuery.data?.teams.map((t) => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                  {reqScopeType === 'department' && scopesQuery.data?.departments.map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                  {reqScopeType === 'organization' && scopesQuery.data?.organizations.map((o) => (
                    <option key={o.id} value={o.id}>{o.name}</option>
                  ))}
                </select>
              </div>

              <div className="form-group">
                <label htmlFor="req-reason">Reason *</label>
                <input
                  id="req-reason"
                  type="text"
                  className="form-control"
                  placeholder="Justification for this position"
                  value={reqReason}
                  onChange={(e) => setReqReason(e.target.value)}
                />
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setShowRequestRoleModal(false)}>Cancel</button>
              <button
                className="btn-primary"
                onClick={() => submitRoleRequestMutation.mutate()}
                disabled={!reqReason.trim() || submitRoleRequestMutation.isPending}
              >
                {submitRoleRequestMutation.isPending ? 'Submitting...' : 'Submit Request'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* =================================================================
          MODAL: Decision (Approve / Reject)
          ================================================================= */}
      {decisionModal && (
        <div className="modal-overlay" onClick={() => setDecisionModal(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>
                {decisionModal.type === 'approve' ? 'Approve Role Request' : 'Reject Role Request'}
              </h3>
              <button className="modal-close-btn" onClick={() => setDecisionModal(null)} aria-label="Close">✕</button>
            </div>
            <div className="modal-body">
              <p style={{ fontSize: '13px', marginBottom: '14px', color: 'var(--text-secondary)' }}>
                Applicant <strong>{decisionModal.req.user_name}</strong> requested role{' '}
                <strong>{decisionModal.req.requested_role}</strong> for scope{' '}
                <strong>{decisionModal.req.scope_name || decisionModal.req.scope_type}</strong>.
              </p>

              <div className="form-group">
                <label htmlFor="decision-comment">
                  {decisionModal.type === 'approve' ? 'Comment (Optional)' : 'Rejection Reason (Required) *'}
                </label>
                <input
                  id="decision-comment"
                  type="text"
                  className="form-control"
                  placeholder={decisionModal.type === 'approve' ? 'Welcome note...' : 'State why request is declined'}
                  value={decisionComment}
                  onChange={(e) => setDecisionComment(e.target.value)}
                />
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn-secondary" onClick={() => setDecisionModal(null)}>Cancel</button>
              <button
                className={decisionModal.type === 'approve' ? 'btn-success' : 'btn-danger'}
                onClick={() => decisionMutation.mutate()}
                disabled={
                  (decisionModal.type === 'reject' && !decisionComment.trim()) ||
                  decisionMutation.isPending
                }
              >
                {decisionMutation.isPending
                  ? 'Recording...'
                  : decisionModal.type === 'approve'
                  ? 'Confirm Approval'
                  : 'Confirm Rejection'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
