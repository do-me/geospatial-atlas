#!/usr/bin/env node
// Cross-platform dispatcher that always runs build.sh via bash.
// On Windows it uses git-bash (preinstalled on GitHub's windows-latest
// runners and on most dev machines via Git for Windows). build.sh is
// POSIX-only; the Windows PowerShell port used to live here but its
// backtick line-continuations were too fragile.
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const script = path.join(__dirname, "build.sh");
const res = spawnSync("bash", [script], { stdio: "inherit" });
process.exit(res.status ?? 1);
