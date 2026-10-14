import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const cli = new URL('../bin/cli.mjs', import.meta.url);
const run = (args, options = {}) => spawnSync(process.execPath, [fileURLToPath(cli), ...args], { encoding: 'utf8', ...options });

test('npx entrypoint supports help and forwards Python arguments and exit codes', () => {
  assert.match(run(['--help']).stdout, /install-skill/);
  const help = run(['open', '--help']);
  assert.equal(help.status, 0, help.stderr);
  assert.match(help.stdout, /--source-thread/);
  assert.notEqual(run(['open', '--unknown']).status, 0);
});

test('installer creates a self-contained skill and protects local changes', () => {
  const directory = mkdtempSync(join(tmpdir(), 'discussion-install-'));
  try {
    const install = run(['install-skill', '--dest', directory]);
    assert.equal(install.status, 0, install.stderr);
    const target = JSON.parse(install.stdout).installed;
    const local = join(target, 'SKILL.md');
    assert.match(readFileSync(local, 'utf8'), /name: codex-parallel-discussion/);
    const helper = spawnSync('python3', ['-B', join(target, 'scripts', 'discussions.py'), '--help'], { encoding: 'utf8' });
    assert.equal(helper.status, 0, helper.stderr);
    writeFileSync(local, 'local customization');
    assert.notEqual(run(['install-skill', '--dest', directory]).status, 0);
    assert.equal(readFileSync(local, 'utf8'), 'local customization');
    const update = run(['install-skill', '--dest', directory, '--force']);
    assert.equal(update.status, 0, update.stderr);
    assert.equal(readFileSync(join(JSON.parse(update.stdout).backup, 'SKILL.md'), 'utf8'), 'local customization');
    assert.match(readFileSync(local, 'utf8'), /name: codex-parallel-discussion/);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
