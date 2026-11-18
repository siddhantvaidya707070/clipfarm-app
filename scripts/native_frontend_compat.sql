-- Native Prisma selects this upstream column during login. The backend-only
-- installation did not create it. Additive and safe to rerun; no records change.
ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_account_id VARCHAR(255);
