# Security policy

## Supported versions

Security fixes are made for the latest published Railmux release. Development
builds and older releases may be useful for diagnosis, but users should upgrade
to the newest stable version before reporting a problem that may already be
fixed.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Email
`zhang.taian@foxmail.com` with the subject `Railmux security report` and include:

- the affected Railmux and tmux versions;
- the local and remote operating systems involved;
- the entry point used (`railmux` or `railmux ssh`);
- a minimal reproduction and the impact you observed; and
- any logs or diagnostics after removing credentials, hostnames, usernames,
  session content, and private paths.

You may run `railmux doctor` or `railmux doctor --remote HOST` for a bounded,
privacy-safe diagnostic summary. Please allow time for the report to be
acknowledged and investigated before public disclosure. Do not include API
keys, SSH credentials, provider transcripts, or other secrets in the report.

## Trust boundaries

Railmux orchestrates existing programs rather than sandboxing the user's
coding agents. Claude Code, Codex, tmux, SSH, the local terminal emulator, and
commands the user explicitly enables retain their own permissions and security
models.

In particular:

- Codex auto-run deliberately bypasses approvals and sandboxing when enabled.
- `railmux ssh` may offer to install a matching package into the remote user's
  environment after explicit consent; it never uses `sudo` or edits shell
  startup files.
- Ask Railmux limits available provider tools for a read-only support session,
  but depends on the installed provider CLI accepting those safety flags and
  fails closed when they are incompatible.
- URL and path actions require an explicit click. Remote paths are revalidated,
  while browsers, Vim, external terminals, and clipboard mechanisms remain
  separate trusted applications.
- OSC 52 clipboard writes and terminal mouse/focus behavior are subject to the
  local terminal's own policy.

The supported platform and terminal boundaries are maintained in
[`docs/SUPPORT_MATRIX.md`](docs/SUPPORT_MATRIX.md).
