INSERT INTO tenants (id, slug, tenant_kind)
VALUES
    ('00000000-0000-0000-0000-000000000002', 'public-demo', 'public_demo'),
    ('00000000-0000-0000-0000-000000000003', 'acceptance', 'acceptance')
ON CONFLICT (id) DO NOTHING;
