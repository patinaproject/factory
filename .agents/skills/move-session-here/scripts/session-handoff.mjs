import { spawnSync } from 'node:child_process';
import {
  existsSync,
  readFileSync,
  realpathSync,
  statSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, isAbsolute, join, resolve } from 'node:path';
import { resolveT3Thread } from './t3-identity.mjs';

const CODEX_LINK = /^codex:\/\/threads\/([A-Fa-f0-9-]+)$/;
const T3_LINK = /^t3:\/\/threads\/([A-Za-z0-9][A-Za-z0-9_-]{2,127})$/;
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$/;
const CODEX_RESPONSE_TYPES = new Set([
  'agent_message',
  'custom_tool_call',
  'custom_tool_call_output',
  'function_call',
  'function_call_output',
  'message',
  'reasoning',
]);

class HandoffError extends Error {
  constructor(code, details, exitCode = 1) {
    super(code);
    this.code = code;
    this.details = details;
    this.exitCode = exitCode;
  }
}

function uniquePaths(paths) {
  const seen = new Set();
  const unique = [];
  for (const path of paths) {
    const absolutePath = resolve(path);
    const identity = existsSync(absolutePath)
      ? realpathSync(absolutePath)
      : absolutePath;
    if (!seen.has(identity)) {
      seen.add(identity);
      unique.push(absolutePath);
    }
  }
  return unique;
}

function findPaths(arguments_) {
  const result = spawnSync('find', [...arguments_, '-print0'], {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) {
    throw new HandoffError('find_failed', { message: result.error.message });
  }
  if (result.status !== 0) {
    throw new HandoffError('find_failed', {
      message: result.stderr.trim(),
      status: result.status,
    });
  }
  return result.stdout.split('\0').filter(Boolean);
}

function requireDirectory(path, variable) {
  if (!isAbsolute(path)) {
    throw new HandoffError('invalid_config_home', {
      variable,
      path,
      message: 'The configured home must be an absolute path.',
    });
  }
  if (!existsSync(path) || !statSync(path).isDirectory()) {
    throw new HandoffError('invalid_config_home', {
      variable,
      path,
      message: 'The configured home must name an existing directory.',
    });
  }
  return resolve(path);
}

function discoverHomes(format) {
  const userHome = process.env.HOME || homedir();
  const namePattern = format === 'claude' ? '.claude*' : '.codex*';
  const overrideVariable =
    format === 'claude' ? 'CLAUDE_CONFIG_DIR' : 'CODEX_HOME';
  const candidates = [];
  const override = process.env[overrideVariable];

  if (override) {
    candidates.push(requireDirectory(override, overrideVariable));
  }
  if (existsSync(userHome) && statSync(userHome).isDirectory()) {
    candidates.push(
      ...findPaths([
        userHome,
        '-maxdepth',
        '1',
        '-type',
        'd',
        '-name',
        namePattern,
        '!',
        '-name',
        '.claude.json*',
      ]).sort()
    );
  }
  return uniquePaths(candidates);
}

function readJsonLines(path) {
  const source = readFileSync(path, 'utf8');
  const lines = source.split('\n');
  if (lines.at(-1) === '') {
    lines.pop();
  }
  return lines.flatMap((line, index) => {
    if (line.trim() === '') {
      return [];
    }
    try {
      const value = JSON.parse(line);
      if (value === null || Array.isArray(value) || typeof value !== 'object') {
        throw new Error('expected a JSON object');
      }
      return [value];
    } catch (error) {
      throw new HandoffError('invalid_transcript', {
        path,
        line: index + 1,
        message: error.message,
      });
    }
  });
}

function lastValue(records, read) {
  for (let index = records.length - 1; index >= 0; index -= 1) {
    const value = read(records[index]);
    if (value !== undefined && value !== null && value !== '') {
      return value;
    }
  }
  return null;
}

function claudeOwnerSessionId(records) {
  return lastValue(records, (record) =>
    typeof record.sessionId === 'string' ? record.sessionId : null
  );
}

function locateClaude(id, validateExactOwner = false) {
  const homes = discoverHomes('claude');
  const projectRoots = homes
    .map((home) => join(home, 'projects'))
    .filter((path) => existsSync(path) && statSync(path).isDirectory());
  const exactMatches = uniquePaths(
    projectRoots.flatMap((root) =>
      findPaths([
        root,
        '-mindepth',
        '2',
        '-maxdepth',
        '2',
        '-type',
        'f',
        '-name',
        `${id}.jsonl`,
      ])
    )
  );
  const ownedExactMatches = validateExactOwner
    ? exactMatches.filter(
        (path) => claudeOwnerSessionId(readJsonLines(path)) === id
      )
    : exactMatches;
  if (ownedExactMatches.length > 1) {
    throw new HandoffError(
      'ambiguous_session',
      {
        format: 'claude',
        id,
        matches: ownedExactMatches,
        searchedHomes: homes,
      },
      3
    );
  }
  if (ownedExactMatches.length === 1) {
    return { path: ownedExactMatches[0], homes };
  }

  const fallbackMatches = [];
  for (const root of projectRoots) {
    const candidates = findPaths([
      root,
      '-mindepth',
      '2',
      '-maxdepth',
      '2',
      '-type',
      'f',
      '-name',
      '*.jsonl',
    ]);
    for (const candidate of candidates) {
      const source = readFileSync(candidate, 'utf8');
      if (!source.includes(id)) {
        continue;
      }
      const records = readJsonLines(candidate);
      if (claudeOwnerSessionId(records) === id) {
        fallbackMatches.push(candidate);
      }
    }
  }
  const uniqueMatches = uniquePaths(fallbackMatches);
  if (uniqueMatches.length > 1) {
    throw new HandoffError(
      'ambiguous_session',
      { format: 'claude', id, matches: uniqueMatches, searchedHomes: homes },
      3
    );
  }
  if (uniqueMatches.length === 0) {
    throw new HandoffError(
      'not_found',
      { format: 'claude', id, searchedHomes: homes },
      2
    );
  }
  return { path: uniqueMatches[0], homes };
}

function claudeChain(records, path) {
  const mainRecords = records.filter((record) => record.isSidechain !== true);
  const byUuid = new Map();
  for (const record of mainRecords) {
    if (typeof record.uuid === 'string' && record.uuid !== '') {
      byUuid.set(record.uuid, record);
    }
  }
  const leafUuid = lastValue(mainRecords, (record) =>
    typeof record.uuid === 'string' ? record.uuid : null
  );
  if (!leafUuid) {
    return [];
  }

  const chain = [];
  const visited = new Set();
  let cursor = leafUuid;
  while (cursor && byUuid.has(cursor)) {
    if (visited.has(cursor)) {
      throw new HandoffError('invalid_transcript', {
        path,
        message: `parentUuid cycle at ${cursor}`,
      });
    }
    visited.add(cursor);
    const record = byUuid.get(cursor);
    chain.unshift(record);
    cursor =
      typeof record.parentUuid === 'string' && record.parentUuid !== ''
        ? record.parentUuid
        : null;
  }
  return chain;
}

function normalizeClaudeTrack(
  path,
  name,
  expectedOwner = null,
  searchedHomes = []
) {
  const records = readJsonLines(path);
  if (expectedOwner && claudeOwnerSessionId(records) !== expectedOwner) {
    throw new HandoffError('not_found', {
      format: 'claude',
      id: expectedOwner,
      searchedHomes,
    }, 2);
  }
  const chain = claudeChain(records, path);
  const events = chain
    .filter((record) => record.type === 'user' || record.type === 'assistant')
    .map((record) => ({
      kind: 'message',
      role: record.type,
      timestamp: record.timestamp ?? null,
      uuid: record.uuid,
      parentUuid: record.parentUuid ?? null,
      content: record.message?.content ?? null,
      ...(Object.hasOwn(record, 'toolUseResult')
        ? { toolUseResult: record.toolUseResult }
        : {}),
    }));
  const activeRecords = records.filter((record) => record.isSidechain !== true);
  const mcp = [];
  for (const record of activeRecords) {
    if (record.attributionMcpServer || record.attributionMcpTool) {
      mcp.push({
        server: record.attributionMcpServer ?? null,
        tool: record.attributionMcpTool ?? null,
      });
    }
  }
  return {
    name,
    path,
    recordCount: records.length,
    events,
    context: {
      cwd: lastValue(activeRecords, (record) => record.cwd),
      gitBranch: lastValue(activeRecords, (record) => record.gitBranch),
      skills: activeRecords.flatMap((record) =>
        typeof record.attributionSkill === 'string'
          ? [record.attributionSkill]
          : []
      ),
      plugins: activeRecords.flatMap((record) =>
        typeof record.attributionPlugin === 'string'
          ? [record.attributionPlugin]
          : []
      ),
      mcp,
      bridgeSessionId: lastValue(activeRecords, (record) =>
        typeof record.bridgeSessionId === 'string'
          ? record.bridgeSessionId
          : null
      ),
    },
  };
}

function distinctStrings(values) {
  return [...new Set(values.filter(Boolean))];
}

function distinctMcp(values) {
  const byIdentity = new Map();
  for (const value of values) {
    const identity = `${value.server ?? ''}\0${value.tool ?? ''}`;
    byIdentity.set(identity, value);
  }
  return [...byIdentity.values()];
}

function extractClaude(id, validateExactOwner = false) {
  const located = locateClaude(id, validateExactOwner);
  const sidecarRoot = join(dirname(located.path), id, 'subagents');
  const sidecars =
    existsSync(sidecarRoot) && statSync(sidecarRoot).isDirectory()
      ? findPaths([sidecarRoot, '-type', 'f', '-name', '*.jsonl']).sort()
      : [];
  const tracks = [
    normalizeClaudeTrack(
      located.path,
      'main',
      validateExactOwner ? id : null,
      located.homes
    ),
    ...sidecars.map((path) =>
      normalizeClaudeTrack(path, `subagent:${basename(path, '.jsonl')}`)
    ),
  ];
  const contexts = tracks.map((track) => track.context);
  return {
    schemaVersion: 1,
    format: 'claude',
    sessionId: id,
    searchedHomes: located.homes,
    sourceFiles: tracks.map((track) => track.path),
    metadata: {
      cwd: tracks[0].context.cwd,
      gitBranch: tracks[0].context.gitBranch,
      skills: distinctStrings(contexts.flatMap((context) => context.skills)),
      plugins: distinctStrings(contexts.flatMap((context) => context.plugins)),
      mcp: distinctMcp(contexts.flatMap((context) => context.mcp)),
      bridgeSessionId: tracks[0].context.bridgeSessionId,
    },
    tracks: tracks.map(({ context: _context, ...track }) => track),
  };
}

function codexSessionId(records) {
  return lastValue(records, (record) =>
    record.type === 'session_meta' &&
    record.payload &&
    typeof record.payload.id === 'string'
      ? record.payload.id
      : null
  );
}

function locateCodex(id) {
  const homes = discoverHomes('codex');
  const matches = [];
  for (const home of homes) {
    const sessions = join(home, 'sessions');
    if (!existsSync(sessions) || !statSync(sessions).isDirectory()) {
      continue;
    }
    for (const candidate of findPaths([
      sessions,
      '-type',
      'f',
      '-name',
      `rollout-*${id}*.jsonl`,
    ])) {
      const records = readJsonLines(candidate);
      if (codexSessionId(records) === id) {
        matches.push(candidate);
      }
    }
  }
  const uniqueMatches = uniquePaths(matches);
  if (uniqueMatches.length > 1) {
    throw new HandoffError(
      'ambiguous_session',
      { format: 'codex', id, matches: uniqueMatches, searchedHomes: homes },
      3
    );
  }
  if (uniqueMatches.length === 0) {
    throw new HandoffError(
      'not_found',
      { format: 'codex', id, searchedHomes: homes },
      2
    );
  }
  return { path: uniqueMatches[0], homes };
}

function sanitizeCodexPayload(payload) {
  const sanitized = { ...payload };
  delete sanitized.encrypted_content;
  delete sanitized.internal_chat_message_metadata_passthrough;
  return sanitized;
}

function extractCodex(id, validateOwner = false) {
  const located = locateCodex(id);
  const records = readJsonLines(located.path);
  if (validateOwner && codexSessionId(records) !== id) {
    throw new HandoffError(
      'not_found',
      { format: 'codex', id, searchedHomes: located.homes },
      2
    );
  }
  const sessionMeta = lastValue(records, (record) =>
    record.type === 'session_meta' && record.payload ? record.payload : null
  );
  const turnContext = lastValue(records, (record) =>
    record.type === 'turn_context' && record.payload ? record.payload : null
  );
  const events = records.flatMap((record) => {
    if (
      record.type === 'response_item' &&
      record.payload &&
      CODEX_RESPONSE_TYPES.has(record.payload.type)
    ) {
      return [
        {
          kind: record.payload.type,
          timestamp: record.timestamp ?? null,
          payload: sanitizeCodexPayload(record.payload),
        },
      ];
    }
    if (record.type === 'compacted') {
      return [
        {
          kind: 'compacted',
          timestamp: record.timestamp ?? null,
          payload: record.payload ?? null,
        },
      ];
    }
    return [];
  });
  return {
    schemaVersion: 1,
    format: 'codex',
    sessionId: id,
    searchedHomes: located.homes,
    sourceFiles: [located.path],
    metadata: {
      cwd: turnContext?.cwd ?? sessionMeta?.cwd ?? null,
      model: turnContext?.model ?? null,
      modelProvider: sessionMeta?.model_provider ?? null,
      effort: turnContext?.effort ?? null,
      git: sessionMeta?.git ?? null,
      workspaceRoots: turnContext?.workspace_roots ?? null,
      source: sessionMeta?.source ?? null,
      originator: sessionMeta?.originator ?? null,
      dynamicTools: sessionMeta?.dynamic_tools ?? null,
      summary: turnContext?.summary ?? null,
    },
    tracks: [
      {
        name: 'main',
        path: located.path,
        recordCount: records.length,
        events,
      },
    ],
  };
}

function parseReference(reference) {
  const t3Match = reference.match(T3_LINK);
  if (t3Match) {
    return { format: 't3', id: t3Match[1] };
  }
  const codexMatch = reference.match(CODEX_LINK);
  if (codexMatch) {
    return { format: 'codex', id: codexMatch[1] };
  }
  if (!SESSION_ID.test(reference)) {
    throw new HandoffError('invalid_session_reference', {
      reference,
      message:
        'Pass one Claude session ID, codex://threads/<id>, or t3://threads/<id>.',
    });
  }
  return { format: 'claude', id: reference };
}

function translateT3ProviderError(error, mapping) {
  const details = {
    t3ThreadId: mapping.t3.threadId,
    databasePath: mapping.t3.databasePath,
    format: mapping.format,
    sessionId: mapping.sessionId,
    ...error.details,
  };
  delete details.id;
  if (error.code === 'not_found') {
    throw new HandoffError('t3_stale_binding', details, 2);
  }
  if (error.code === 'ambiguous_session') {
    throw new HandoffError('t3_ambiguous_binding', details, 3);
  }
  throw new HandoffError(error.code, details, error.exitCode);
}

async function main() {
  if (process.argv.length !== 3 || process.argv[2] === '--help') {
    process.stdout.write(
      'Usage: node session-handoff.mjs <claude-session-id|codex://threads/id|t3://threads/id>\n'
    );
    process.exitCode = process.argv[2] === '--help' ? 0 : 1;
    return;
  }
  const reference = parseReference(process.argv[2]);
  let result;
  if (reference.format === 't3') {
    const mapping = await resolveT3Thread(reference.id);
    try {
      result =
        mapping.format === 'claude'
          ? extractClaude(mapping.sessionId, true)
          : extractCodex(mapping.sessionId, true);
    } catch (error) {
      if (error instanceof HandoffError) {
        translateT3ProviderError(error, mapping);
      }
      throw error;
    }
    result.t3 = mapping.t3;
  } else {
    result =
      reference.format === 'claude'
        ? extractClaude(reference.id)
        : extractCodex(reference.id);
  }
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

try {
  await main();
} catch (error) {
  if (error instanceof HandoffError || error?.name === 'T3IdentityError') {
    process.stderr.write(
      `${JSON.stringify({ error: error.code, ...error.details }, null, 2)}\n`
    );
    process.exitCode = error.exitCode;
  } else {
    throw error;
  }
}
