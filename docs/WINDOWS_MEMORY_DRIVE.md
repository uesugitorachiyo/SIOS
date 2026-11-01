# Windows Memory drive

A Windows user can edit the existing Mac-hosted SIOS Memory tree through an
SSHFS drive. The drive is a view of the central files, not an independent
copy; saving changes the Mac file directly. The SIOS index reconciles the
central Memory tree while the central service runs. This is not an offline
synchronization system and does not resolve concurrent edits. Avoid editing
one file in two applications at once.

The optional `scripts/windows_memory_mount.py` launcher accepts a private JSON
configuration with `host_alias`, `remote_path`, and `drive`. Store it outside
this source checkout. The alias must refer to an existing verified SSH host and
key. The launcher requires strict host-key checking and batch authentication,
uses `sshfs-win cmd`, disables directory caching, and reconnects without
collecting file contents. It never replaces an occupied drive. WinFsp and
SSHFS-Win must already be installed. Do not use SSHFS-Win's default `svc`
mode for this connection: its documented implementation disables host-key
checking. Launch the script in the user's interactive Windows session, since a
drive mounted by an SSH service session is not visible in the desktop session.
A per-user Startup shortcut can launch it at logon; remove the shortcut to
stop automatic future launches. Network loss can make the drive unavailable;
it is not a cue to create or edit a local replacement copy.

Run the launcher with an installed Windows Python 3 interpreter, for example
`pythonw.exe windows_memory_mount.py config.json`. Keep both files in a private
per-user directory. The JSON file must contain only the three string values,
such as `{"host_alias": "sios-mac", "remote_path": "/absolute/path/to/Memory", "drive": "M:"}`.
The launcher writes a small `status.json` beside the configuration, without
file contents. It retries a failed connection and does not change the Windows
PowerShell execution policy. When the connection or central service is down,
the drive or search index can be temporarily unavailable. The status reports
the launcher's last observed mount state; it is not a guarantee that the
network or search index is currently available.

The launcher allows extra time for a slow Windows sign-in and, if the initial
mount times out, stops only the SSHFS process tree it started before retrying.
An occupied drive is never replaced; investigate `drive_in_use` before taking
any action against another process or drive.

The tested SSHFS-Win command-line runtime cannot mount a remote root path
that combines non-ASCII characters with whitespace; the launcher rejects
such a configuration instead of repeatedly failing. Unicode file and folder
names containing spaces under a supported mounted root were tested separately.
