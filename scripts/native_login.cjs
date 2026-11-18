// Run once inside the native frontend with its normal database configuration.
// Input is a private JSON file on stdin; credentials never appear in argv/logs.
const fs = require('fs');
const crypto = require('crypto');
let stage = 'password helper';
async function main() {
  process.env.DATABASE_URL ||= `postgresql://supoclip:${encodeURIComponent(process.env.POSTGRES_PASSWORD)}@postgres:5432/supoclip`;
  const { hashPassword } = await import('better-auth/crypto');
  stage = 'database connection';
  const { PrismaClient } = require('/app/src/generated/prisma');
  const db = new PrismaClient();
  try {
    stage = 'private input';
    const input = JSON.parse(fs.readFileSync(0, 'utf8').replace(/^\uFEFF/, ''));
    stage = 'owner lookup';
    const user = await db.user.findUniqueOrThrow({ where: { id: 'clipfarm-owner' } });
    if (user.email !== input.email) throw new Error('Owner mismatch');
    const existing = await db.account.findFirst({ where: { userId: user.id, providerId: 'credential' } });
    if (!existing) {
      stage = 'credential creation';
      await db.account.create({ data: { id: crypto.randomUUID(), accountId: user.id,
        userId: user.id, providerId: 'credential', password: await hashPassword(input.password),
        createdAt: new Date(), updatedAt: new Date() } });
    }
    stage = 'owner access';
    await db.user.update({ where: { id: user.id }, data: { is_admin: true, emailVerified: true } });
    console.log('Existing owner enabled for native login; original task ownership preserved.');
  } finally { await db.$disconnect(); }
}
main().catch((error) => { console.error('Native login setup failed at', stage, error.name, error.code || '', JSON.stringify(error.meta || {}), '; no credentials printed.'); process.exitCode = 1; });
