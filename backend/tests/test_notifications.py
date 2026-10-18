import uuid
import pytest
from fastapi.testclient import TestClient

from app.db import get_db_connection
from app.main import app
from app.repositories.identity_repo import create_user
from app.repositories.notifications_repo import create_notification, get_unread_count


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_get_notifications_requires_authentication(client):
    """Verify notification endpoints strictly require authentication."""
    res = client.get("/notifications")
    assert res.status_code == 401

    res_cnt = client.get("/notifications/unread-count")
    assert res_cnt.status_code == 401


def test_notifications_user_isolation(client):
    """Verify users can only access their own notifications, receiving 404 for others' items."""
    with get_db_connection() as conn:
        with conn.transaction():
            user_a = create_user(conn, email=f"alice-{uuid.uuid4().hex[:8]}@example.com", display_name="Alice")
            user_b = create_user(conn, email=f"bob-{uuid.uuid4().hex[:8]}@example.com", display_name="Bob")

            # Notification for Alice
            notif_a = create_notification(
                conn,
                user_id=user_a.id,
                event_id=None,
                dedupe_key=f"isolation-test-{uuid.uuid4()}",
                notification_type="work_item_assigned",
                title="Confidential Task",
                message="Alice secret task",
            )
            assert notif_a is not None

    # Bob lists notifications -> Alice's notification is not present
    res_b = client.get(
        "/notifications",
        headers={"X-Dev-User-Email": user_b.email},
    )
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert len(data_b["notifications"]) == 0
    assert data_b["unread_count"] == 0

    # Bob attempts to read Alice's notification -> 404 Not Found (information hiding)
    res_read = client.post(
        f"/notifications/{notif_a.id}/read",
        headers={"X-Dev-User-Email": user_b.email},
    )
    assert res_read.status_code == 404
    assert "not found" in res_read.json()["detail"].lower()


def test_unread_count_mark_read_and_read_all(client):
    """Verify unread counter accurately tracks state and mark-as-read is idempotent."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"reader-{uuid.uuid4().hex[:8]}@example.com", display_name="Reader User")

            n1 = create_notification(
                conn, user_id=user.id, event_id=None,
                dedupe_key=f"unread-1-{uuid.uuid4()}", notification_type="task",
                title="Notification 1", message="Message 1",
            )
            n2 = create_notification(
                conn, user_id=user.id, event_id=None,
                dedupe_key=f"unread-2-{uuid.uuid4()}", notification_type="task",
                title="Notification 2", message="Message 2",
            )
            n3 = create_notification(
                conn, user_id=user.id, event_id=None,
                dedupe_key=f"unread-3-{uuid.uuid4()}", notification_type="task",
                title="Notification 3", message="Message 3",
            )

    headers = {"X-Dev-User-Email": user.email}

    # 1. Initial count is 3
    cnt_res = client.get("/notifications/unread-count", headers=headers)
    assert cnt_res.status_code == 200
    assert cnt_res.json()["unread_count"] == 3

    # 2. Mark n1 as read
    read_res = client.post(f"/notifications/{n1.id}/read", headers=headers)
    assert read_res.status_code == 200
    assert read_res.json()["read_at"] is not None

    # Idempotent re-mark: still 200
    read_again = client.post(f"/notifications/{n1.id}/read", headers=headers)
    assert read_again.status_code == 200

    # Unread count is now 2
    cnt_res2 = client.get("/notifications/unread-count", headers=headers)
    assert cnt_res2.json()["unread_count"] == 2

    # 3. Mark all read
    all_res = client.post("/notifications/read-all", headers=headers)
    assert all_res.status_code == 200
    assert all_res.json()["marked_read_count"] == 2

    # Unread count is now 0
    cnt_res3 = client.get("/notifications/unread-count", headers=headers)
    assert cnt_res3.json()["unread_count"] == 0


def test_notification_keyset_cursor_pagination(client):
    """Verify newest-first keyset cursor pagination with opaque cursor."""
    with get_db_connection() as conn:
        with conn.transaction():
            user = create_user(conn, email=f"page-{uuid.uuid4().hex[:8]}@example.com", display_name="Page User")
            for i in range(5):
                create_notification(
                    conn,
                    user_id=user.id,
                    event_id=None,
                    dedupe_key=f"page-item-{i}-{uuid.uuid4()}",
                    notification_type="task",
                    title=f"Notification #{i}",
                    message=f"Message body #{i}",
                )

    headers = {"X-Dev-User-Email": user.email}

    # Fetch page 1 (limit = 2)
    p1 = client.get("/notifications?limit=2", headers=headers)
    assert p1.status_code == 200
    d1 = p1.json()
    assert len(d1["notifications"]) == 2
    assert d1["has_more"] is True
    assert d1["next_cursor"] is not None

    # Fetch page 2 using cursor
    p2 = client.get(f"/notifications?limit=2&cursor={d1['next_cursor']}", headers=headers)
    assert p2.status_code == 200
    d2 = p2.json()
    assert len(d2["notifications"]) == 2
    assert d2["has_more"] is True

    # Confirm no overlap between pages
    ids_p1 = {n["id"] for n in d1["notifications"]}
    ids_p2 = {n["id"] for n in d2["notifications"]}
    assert ids_p1.isdisjoint(ids_p2)

    # Invalid cursor format returns 422
    bad_cursor = client.get("/notifications?cursor=INVALID_CURSOR!!", headers=headers)
    assert bad_cursor.status_code == 422
