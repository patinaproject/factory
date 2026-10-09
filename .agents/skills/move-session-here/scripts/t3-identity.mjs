import { spawnSync } from 'node:child_process';
import { existsSync, realpathSync, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$/;

class T3IdentityError extends Error {
  constructor(code, details, exitCode = 1) {
    super(code);
    this.name = 'T3IdentityError';
    this.code = code;
    this.details = details;
    this.exitCode = exitCode;
  }
}

function selectDatabasePath() {
  const override = process.env.T3_STATE_DB;
  const databasePath = override
    ? override
    : join(process.env.HOME || homedir(), '.t3', 'userdata', 'state.sqlite');
  if (override && !isAbsolute(override)) {
    throw new T3IdentityError('invalid_t3_store', {
      variable: 'T3_STATE_DB',
      path: override,
      message: 'T3_STATE_DB must be an absolute path to a regular file.',
    });
  }
  const absolutePath = resolve(databasePath);
  if (!existsSync(absolutePath)) {
    throw new T3IdentityError(
      't3_store_not_found',
      { databasePath: absolutePath },
      2
    );
  }
  if (!statSync(absolutePath).isFile()) {
    throw new T3IdentityError('invalid_t3_store', {
      variable: override ? 'T3_STATE_DB' : undefined,
      path: absolutePath,
      message: 'The T3 state database must be a regular file.',
    });
  }
  return realpathSync(absolutePath);
}

function parseCursor(row, threadId, databasePath) {
  let cursor;
  try {
    cursor = JSON.parse(row.resume_cursor_json);
  } catch {
    throw new T3IdentityError('t3_invalid_mapping', {
      threadId,
      databasePath,
      message: 'The provider resume cursor is not valid JSON.',
    });
  }
  if (cursor === null || Array.isArray(cursor) || typeof cursor !== 'object') {
    throw new T3IdentityError('t3_invalid_mapping', {
      threadId,
      databasePath,
      message: 'The provider resume cursor must be a JSON object.',
    });
  }
  return cursor;
}

function requireSessionId(value, threadId, databasePath) {
  if (typeof value !== 'string' || !SESSION_ID.test(value)) {
    throw new T3IdentityError('t3_invalid_mapping', {
      threadId,
      databasePath,
      message: 'The provider session identity is invalid.',
    });
  }
  return value;
}

function validateOptionalSessionId(value, threadId, databasePath) {
  if (value !== undefined && value !== null) {
    requireSessionId(value, threadId, databasePath);
  }
}

function mapRuntimeRow(row, threadId, databasePath) {
  const cursor = parseCursor(row, threadId, databasePath);
  if (row.provider_name === 'claudeAgent' && row.adapter_key === 'claudeAgent') {
    if (cursor.threadId !== threadId) {
      throw new T3IdentityError('t3_invalid_mapping', {
        threadId,
        databasePath,
        message: 'The Claude cursor belongs to a different T3 thread.',
      });
    }
    validateOptionalSessionId(cursor.resume, threadId, databasePath);
    validateOptionalSessionId(cursor.sessionId, threadId, databasePath);
    if (
      typeof cursor.resume === 'string' &&
      typeof cursor.sessionId === 'string' &&
      cursor.resume !== cursor.sessionId
    ) {
      throw new T3IdentityError('t3_invalid_mapping', {
        threadId,
        databasePath,
        message: 'The Claude cursor contains contradictory session identities.',
      });
    }
    const sessionId = requireSessionId(
      cursor.resume ?? cursor.sessionId,
      threadId,
      databasePath
    );
    return { format: 'claude', sessionId };
  }
  if (row.provider_name === 'codex' && row.adapter_key === 'codex') {
    return {
      format: 'codex',
      sessionId: requireSessionId(cursor.threadId, threadId, databasePath),
    };
  }
  throw new T3IdentityError('t3_unsupported_provider', {
    threadId,
    databasePath,
    providerName: row.provider_name,
    adapterKey: row.adapter_key,
  });
}

function queryRuntimeRows(databasePath, threadId) {
  const threadHex = Buffer.from(threadId, 'utf8').toString('hex');
  const result = spawnSync(
    'sqlite3',
    ['-init', '/dev/null', '-batch', '-bail', '-readonly', '-json', '--', databasePath],
    {
      encoding: 'utf8',
      maxBuffer: 1024 * 1024,
      input: `BEGIN;
SELECT count(*) AS required_columns
FROM pragma_table_info('provider_session_runtime')
WHERE name IN ('thread_id', 'provider_name', 'adapter_key', 'resume_cursor_json');
SELECT
  CASE WHEN typeof(thread_id) = 'text' THEN thread_id END AS thread_id,
  CASE WHEN typeof(provider_name) = 'text' THEN provider_name END AS provider_name,
  CASE WHEN typeof(adapter_key) = 'text' THEN adapter_key END AS adapter_key,
  CASE WHEN typeof(resume_cursor_json) = 'text' THEN resume_cursor_json END AS resume_cursor_json
FROM provider_session_runtime
WHERE thread_id = CAST(X'${threadHex}' AS TEXT)
LIMIT 2;
COMMIT;
`,
    }
  );
  if (
    ['ENOENT', 'EACCES', 'ENOEXEC'].includes(result.error?.code) ||
    /(?:unknown|unrecognized) option:.*(?:json|readonly|init)/i.test(result.stderr)
  ) {
    throw new T3IdentityError('t3_sqlite_unavailable', {
      databasePath,
      message: 'Install a current upstream SQLite CLI (3.33.0 or later) and ensure sqlite3 is on PATH.',
    });
  }
  try {
    if (result.error) throw result.error;
    const separator = result.stdout.indexOf('\n');
    const schema = JSON.parse(result.stdout.slice(0, separator));
    if (
      !Array.isArray(schema) || schema.length !== 1 ||
      !Number.isInteger(schema[0]?.required_columns)
    ) {
      throw new Error('Invalid schema result');
    }
    if (schema[0].required_columns !== 4) {
      throw new T3IdentityError('t3_unsupported_schema', { databasePath });
    }
    if (result.status !== 0 || result.stderr) throw new Error('SQLite read failed');
    const rows = JSON.parse(result.stdout.slice(separator + 1).trim() || '[]');
    if (!Array.isArray(rows) || rows.length > 2) {
      throw new Error('Invalid row result');
    }
    for (const row of rows) {
      if (row?.thread_id !== threadId) {
        throw new T3IdentityError('t3_invalid_mapping', {
          threadId,
          databasePath,
          message: 'The provider binding belongs to a different T3 thread.',
        });
      }
    }
    return rows;
  } catch (error) {
    if (error instanceof T3IdentityError) throw error;
    throw new T3IdentityError('invalid_t3_store', {
      databasePath,
      message: 'The T3 state database could not be read. Check file access and use a current upstream sqlite3 on PATH; query output must fit within 1 MiB.',
    });
  }
}

function resolveThreadFromStore(threadId) {
  const databasePath = selectDatabasePath();
  if (existsSync(`${databasePath}-journal`)) {
    throw new T3IdentityError('t3_rollback_journal', { databasePath });
  }
  const rows = queryRuntimeRows(databasePath, threadId);
  if (rows.length === 0) {
    throw new T3IdentityError(
      't3_mapping_not_found',
      { threadId, databasePath },
      2
    );
  }
  if (rows.length > 1) {
    throw new T3IdentityError(
      't3_mapping_ambiguous',
      { threadId, databasePath },
      3
    );
  }
  const mapping = mapRuntimeRow(rows[0], threadId, databasePath);
  return {
    ...mapping,
    t3: { threadId, databasePath },
  };
}

export async function resolveT3Thread(threadId) {
  try {
    return await resolveThreadFromStore(threadId);
  } catch (error) {
    if (error instanceof T3IdentityError) {
      throw error;
    }
    throw new T3IdentityError('t3_runtime_error', {
      threadId,
      message: 'The T3 identity lookup failed in this runtime.',
    });
  }
}
