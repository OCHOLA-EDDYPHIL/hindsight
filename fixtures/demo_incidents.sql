INSERT INTO services (id, slug, name, owner_team, tier)
VALUES
    (
        '10000000-0000-0000-0000-000000000001',
        'payments-api',
        'Payments API',
        'revenue-platform',
        'critical'
    ),
    (
        '10000000-0000-0000-0000-000000000002',
        'edge-gateway',
        'Edge Gateway',
        'platform-edge',
        'critical'
    ),
    (
        '10000000-0000-0000-0000-000000000003',
        'orders-worker',
        'Orders Worker',
        'fulfillment',
        'core'
    )
ON CONFLICT (slug) DO UPDATE SET
    name = excluded.name,
    owner_team = excluded.owner_team,
    tier = excluded.tier;

INSERT INTO runbooks (id, slug, service_id, title, summary, steps)
VALUES
    (
        '20000000-0000-0000-0000-000000000001',
        'payments-latency-triage',
        '10000000-0000-0000-0000-000000000001',
        'Payments latency triage',
        'Check downstream processor latency, queue depth, and retry pressure before scaling workers.',
        '[
            "Check p95 and p99 latency on payments-api.",
            "Compare downstream processor timeout rate to baseline.",
            "Throttle retry fanout if queue depth is compounding latency.",
            "Scale workers only after the downstream timeout is mitigated."
        ]'::JSONB
    ),
    (
        '20000000-0000-0000-0000-000000000002',
        'edge-certificate-rotation',
        '10000000-0000-0000-0000-000000000002',
        'Edge certificate rotation',
        'Rotate the edge certificate, verify chain propagation, and invalidate stale gateway config.',
        '[
            "Confirm certificate expiry and affected hostnames.",
            "Install the renewed certificate bundle.",
            "Reload edge gateway configuration.",
            "Verify TLS handshake from two external regions."
        ]'::JSONB
    ),
    (
        '20000000-0000-0000-0000-000000000003',
        'orders-worker-crashloop',
        '10000000-0000-0000-0000-000000000003',
        'Orders worker crash loop',
        'Pause the consumer, clear the poison message, and restart the worker deployment.',
        '[
            "Pause orders queue consumption.",
            "Inspect the latest failed message payload.",
            "Move the poison message to quarantine.",
            "Restart the worker and resume consumption."
        ]'::JSONB
    )
ON CONFLICT (slug) DO UPDATE SET
    service_id = excluded.service_id,
    title = excluded.title,
    summary = excluded.summary,
    steps = excluded.steps;

INSERT INTO incidents (
    id, slug, title, severity, status, started_at, resolved_at, summary, root_cause
)
VALUES
    (
        '30000000-0000-0000-0000-000000000001',
        'inc-payment-latency-2026-06-14',
        'Payment authorization latency above checkout SLO',
        'sev2',
        'resolved',
        '2026-06-14T09:12:00Z',
        '2026-06-14T09:47:00Z',
        'Checkout requests slowed when the payment processor timeout rate spiked and retries saturated workers.',
        'Retry fanout amplified downstream payment processor timeouts.'
    ),
    (
        '30000000-0000-0000-0000-000000000002',
        'inc-edge-cert-expiry-2026-06-21',
        'Expired certificate served by edge gateway',
        'sev1',
        'resolved',
        '2026-06-21T02:03:00Z',
        '2026-06-21T02:24:00Z',
        'A stale certificate bundle remained active on one edge gateway shard after rotation.',
        'Gateway config reload did not propagate the renewed certificate to one shard.'
    ),
    (
        '30000000-0000-0000-0000-000000000003',
        'inc-orders-worker-crashloop-2026-06-29',
        'Orders worker crash loop on malformed fulfillment event',
        'sev3',
        'resolved',
        '2026-06-29T16:18:00Z',
        '2026-06-29T17:06:00Z',
        'A malformed fulfillment event caused repeated worker crashes until the message was quarantined.',
        'A poison message bypassed schema validation and crashed the worker on deserialization.'
    )
ON CONFLICT (slug) DO UPDATE SET
    title = excluded.title,
    severity = excluded.severity,
    status = excluded.status,
    started_at = excluded.started_at,
    resolved_at = excluded.resolved_at,
    summary = excluded.summary,
    root_cause = excluded.root_cause;

INSERT INTO incident_services (incident_id, service_id, impact)
VALUES
    (
        '30000000-0000-0000-0000-000000000001',
        '10000000-0000-0000-0000-000000000001',
        'Checkout authorization latency exceeded SLO.'
    ),
    (
        '30000000-0000-0000-0000-000000000002',
        '10000000-0000-0000-0000-000000000002',
        'TLS failures blocked a subset of public API clients.'
    ),
    (
        '30000000-0000-0000-0000-000000000003',
        '10000000-0000-0000-0000-000000000003',
        'Order fulfillment lag increased while the consumer was crash-looping.'
    )
ON CONFLICT (incident_id, service_id) DO UPDATE SET
    impact = excluded.impact;

INSERT INTO incident_runbooks (incident_id, runbook_id, usage_note, outcome)
VALUES
    (
        '30000000-0000-0000-0000-000000000001',
        '20000000-0000-0000-0000-000000000001',
        'Used latency triage to separate processor timeout from worker capacity.',
        'Retries were throttled before worker scaling, reducing queue pressure.'
    ),
    (
        '30000000-0000-0000-0000-000000000002',
        '20000000-0000-0000-0000-000000000002',
        'Used certificate rotation checks to identify the stale edge shard.',
        'Reloaded gateway config and verified the renewed certificate externally.'
    ),
    (
        '30000000-0000-0000-0000-000000000003',
        '20000000-0000-0000-0000-000000000003',
        'Used crash-loop runbook to pause consumption and quarantine the poison message.',
        'Worker restarted cleanly after the malformed event was removed.'
    )
ON CONFLICT (incident_id, runbook_id) DO UPDATE SET
    usage_note = excluded.usage_note,
    outcome = excluded.outcome;

INSERT INTO incident_events (id, incident_id, occurred_at, event_type, summary, metadata)
VALUES
    (
        '40000000-0000-0000-0000-000000000001',
        '30000000-0000-0000-0000-000000000001',
        '2026-06-14T09:12:00Z',
        'alert',
        'Checkout p99 latency breached SLO.',
        '{"signal": "checkout_p99_latency"}'::JSONB
    ),
    (
        '40000000-0000-0000-0000-000000000002',
        '30000000-0000-0000-0000-000000000001',
        '2026-06-14T09:29:00Z',
        'mitigation',
        'Retry fanout was throttled while processor timeout rate recovered.',
        '{"action": "throttle_retries"}'::JSONB
    ),
    (
        '40000000-0000-0000-0000-000000000003',
        '30000000-0000-0000-0000-000000000002',
        '2026-06-21T02:03:00Z',
        'alert',
        'Synthetic TLS checks failed against one edge shard.',
        '{"signal": "tls_handshake_failure"}'::JSONB
    ),
    (
        '40000000-0000-0000-0000-000000000004',
        '30000000-0000-0000-0000-000000000002',
        '2026-06-21T02:18:00Z',
        'mitigation',
        'Gateway configuration was reloaded with the renewed certificate bundle.',
        '{"action": "reload_gateway_config"}'::JSONB
    ),
    (
        '40000000-0000-0000-0000-000000000005',
        '30000000-0000-0000-0000-000000000003',
        '2026-06-29T16:18:00Z',
        'alert',
        'Orders worker entered crash loop after consuming a malformed event.',
        '{"signal": "pod_crash_loop"}'::JSONB
    ),
    (
        '40000000-0000-0000-0000-000000000006',
        '30000000-0000-0000-0000-000000000003',
        '2026-06-29T16:52:00Z',
        'mitigation',
        'Malformed event was quarantined and the worker deployment restarted.',
        '{"action": "quarantine_poison_message"}'::JSONB
    )
ON CONFLICT (id) DO UPDATE SET
    incident_id = excluded.incident_id,
    occurred_at = excluded.occurred_at,
    event_type = excluded.event_type,
    summary = excluded.summary,
    metadata = excluded.metadata;
