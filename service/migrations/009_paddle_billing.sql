CREATE UNIQUE INDEX top_up_orders_paddle_reference_unique
    ON top_up_orders(provider_reference)
    WHERE provider = 'paddle' AND provider_reference IS NOT NULL;
