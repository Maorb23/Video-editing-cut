CREATE TABLE user_profiles (
    user_id text PRIMARY KEY,
    avatar_key text NOT NULL DEFAULT 'camera',
    email_verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE credit_ledger_entries (
    id text PRIMARY KEY,
    user_id text NOT NULL,
    amount integer NOT NULL CHECK (amount <> 0),
    reason text NOT NULL CHECK (reason IN ('purchase','generation','refund','promotion','adjustment')),
    edit_id text REFERENCES edits(id),
    payment_reference text,
    idempotency_key text,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id, idempotency_key)
);
CREATE INDEX credit_ledger_user_created ON credit_ledger_entries(user_id, created_at DESC);

CREATE TABLE top_up_orders (
    id text PRIMARY KEY,
    user_id text NOT NULL,
    package_key text NOT NULL,
    credits integer NOT NULL CHECK (credits > 0),
    price_minor integer NOT NULL CHECK (price_minor > 0),
    currency text NOT NULL CHECK (length(currency) = 3),
    status text NOT NULL CHECK (status IN ('pending','succeeded','failed')),
    provider text NOT NULL DEFAULT 'mock',
    provider_reference text,
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX top_up_orders_user_created ON top_up_orders(user_id, created_at DESC);
