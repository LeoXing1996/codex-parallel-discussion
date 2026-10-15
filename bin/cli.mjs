#!/usr/bin/env node
import { cpSync, existsSync, mkdirSync, readFileSync, renameSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const skill = join(root, 'skills', 'codex-parallel-discussion');
const args = process.argv.slice(2);
const usage = `Codex Parallel Discussion

Usage:
  codex-parallel-discussion install-skill [--dest <skills-directory>] [--force]
  codex-parallel-discussion preflight [--parent-pane %N] [options]
  codex-parallel-discussion open --items <topics.json> [--parent-pane %N] [options]
  codex-parallel-discussion status --state <state.json>
  codex-parallel-discussion collect --state <state.json> [--lines 120]
  codex-parallel-discussion close --state <state.json>

One list item creates one interactive Codex fork in one tmux pane.
Run 'open --help' for all flags. Runtime: Python 3.9+, tmux, Codex CLI.
install-skill defaults to $CODEX_HOME/skills or ~/.codex/skills.
--force backs up an existing installation before replacing it.
`;

try {
  if (!args.length || ['--help', '-h', 'help'].includes(args[0])) {
    console.log(usage);
  } else if (['--version', '-v'].includes(args[0])) {
    console.log(JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).version);
  } else if (args[0] === 'install-skill') {
    let destination = join(process.env.CODEX_HOME || join(homedir(), '.codex'), 'skills');
    let force = false;
    for (let index = 1; index < args.length; index++) {
      if (args[index] === '--force') force = true;
      else if (args[index] === '--dest' && args[index + 1] && !args[index + 1].startsWith('--')) {
        destination = resolve(args[++index]);
      } else throw new Error(`Unknown or incomplete install option: ${args[index]}`);
    }
    const target = join(resolve(destination), 'codex-parallel-discussion');
    if (target === skill) throw new Error('Source and destination are the same directory.');
    let backup;
    if (existsSync(target)) {
      if (!force) throw new Error(`Skill already exists at ${target}. Use --force to back it up and replace it.`);
      backup = join(dirname(resolve(destination)), 'skill-backups', `codex-parallel-discussion-${Date.now()}`);
      mkdirSync(dirname(backup), { recursive: true });
      renameSync(target, backup);
    }
    mkdirSync(dirname(target), { recursive: true });
    cpSync(skill, target, { recursive: true, errorOnExist: true, force: false });
    console.log(JSON.stringify({ installed: target, ...(backup ? { backup } : {}) }, null, 2));
  } else {
    const child = spawn('python3', ['-B', join(skill, 'scripts', 'discussions.py'), ...args], { stdio: 'inherit' });
    child.on('error', error => {
      console.error(error.code === 'ENOENT' ? 'Python 3.9+ is required. Install python3 and try again.' : error.message);
      process.exitCode = 1;
    });
    child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
