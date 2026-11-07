# Backups with SQLite WAL

The backup takes an SQLite snapshot while the Store coordinates writes. Before
allocating the in-memory copy, it checks the database's logical page count and
page size as well as the archive size and available-memory guard. It checks the
snapshot's actual size and integrity before writing an encrypted archive.

A completed WAL-mode image is normalized only in a private snapshot used for a
new archive. When restoring an older archive, the archived database bytes remain
unchanged; a normalized copy is used for in-memory validation. This preserves
existing authenticated archives rather than rewriting them during validation.

These checks rely on a consistent SQLite database and cooperative locking. A
corrupt or externally modified database that reports an incorrect page count
can still allocate memory before the size mismatch is detected. Independent
processes, sudden power loss, real-data recovery, and every platform are not
qualified by the synthetic WAL tests. Keep separate backups and validate the
actual recovery procedure for the deployment.
