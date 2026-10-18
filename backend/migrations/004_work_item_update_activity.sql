ALTER TABLE activity
    DROP CONSTRAINT IF EXISTS activity_event_type_check;

ALTER TABLE activity
    ADD CONSTRAINT chk_activity_event_type CHECK (
        event_type IN ('created', 'comment', 'status_changed', 'assigned', 'claimed', 'released', 'updated')
    );
