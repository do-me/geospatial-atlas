#!/usr/bin/env node
// Cross-platform dispatcher that runs build.sh on Unix and build.ps1 on Windows.
const { spawnSync } = require("node:child_process");
const path = require("node:path");

const here = __dirname;
const isWindows = process.platform === "win32";

const cmd = isWindows ? "powershell" : "bash";
const args = isWindows
  ? ["-ExecutionPolicy", "Bypass", "-File", path.join(here, "build.ps1")]
  : [path.join(here, "build.sh")];

const res = spawnSync(cmd, args, { stdio: "inherit" });
process.exit(res.status ?? 1);
