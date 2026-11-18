"""Switch between provisioned Hindsight writer roles inside a protected host.

The root-owned supervisor must stop both application services before calling
these operations. Clients cannot call these operations directly. The two
services use different identities in the immutable PostgreSQL peer map.
"""

from contextlib import contextmanager

from .protected_postgres_access import production_access_files


BANK_DATABASE = 'sios_hindsight_migration'
HOST_ADMIN = 'sios_host_admin'
APP_WRITER = 'sios_app_writer'
RECOVERY_WRITER = 'sios_recovery_writer'
WRITERS = {'application': APP_WRITER, 'recovery': RECOVERY_WRITER}
TRANSITION_LOCK = 738048720104974830


class WriterTransition:
    def __init__(self, connection, system_identifier):
        if not isinstance(system_identifier, str) or not system_identifier.isascii() or not system_identifier.isdigit():
            raise ValueError('a pinned PostgreSQL system identifier is required')
        self.connection = connection
        self.system_identifier = system_identifier

    @contextmanager
    def _control(self):
        result = self.connection.query(
            "SELECT current_database(), session_user, current_user, system_identifier::text, "
            "inet_client_addr() IS NULL, current_setting('transaction_read_only') "
            "FROM pg_catalog.pg_control_system()"
        )
        if result != [[BANK_DATABASE, HOST_ADMIN, HOST_ADMIN, self.system_identifier, 't', 'off']]:
            raise ValueError('protected PostgreSQL administrator identity did not match')
        if self.connection.scalar(f'SELECT pg_catalog.pg_try_advisory_lock({TRANSITION_LOCK})') != 't':
            raise ValueError('another writer transition is active')
        try:
            yield
        finally:
            self.connection.query(f'SELECT pg_catalog.pg_advisory_unlock({TRANSITION_LOCK})')

    def _configured_logins(self):
        roles = self.connection.query(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles ORDER BY rolname"
        )
        login_roles = set()
        writers = set()
        for name, login, *privileged in roles:
            if login == 't':
                login_roles.add(name)
            if name in WRITERS.values():
                writers.add(name)
                if privileged != ['f'] * 5:
                    raise ValueError('writer role has administrative attributes')
        if writers != set(WRITERS.values()) or HOST_ADMIN not in login_roles:
            raise ValueError('protected roles are missing')
        if not login_roles <= {HOST_ADMIN, *writers}:
            raise ValueError('unexpected role can log in')
        escalation = self.connection.scalar(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles writer CROSS JOIN pg_catalog.pg_roles privileged "
            f"WHERE writer.rolname IN ('{APP_WRITER}','{RECOVERY_WRITER}') "
            "AND (privileged.rolsuper OR privileged.rolcreaterole OR privileged.rolcreatedb OR "
            "privileged.rolreplication OR privileged.rolbypassrls OR privileged.rolname LIKE 'pg\\_%' "
            "OR privileged.rolname IN "
            f"('{APP_WRITER}','{RECOVERY_WRITER}')) AND writer.oid <> privileged.oid "
            "AND (pg_catalog.pg_has_role(writer.oid,privileged.oid,'USAGE') "
            "OR pg_catalog.pg_has_role(writer.oid,privileged.oid,'SET')))"
        )
        if escalation != 'f':
            raise ValueError('writer role has unsafe or unverified role membership')
        return login_roles

    def _prove_drained(self, writer=None):
        allowed = [HOST_ADMIN] if writer is None else [HOST_ADMIN, writer]
        matching_roles = ','.join(self.connection.literal(name) for name in allowed)
        queries = (
            "SELECT count(*) FROM pg_catalog.pg_stat_activity "
            "WHERE backend_type IN ('client backend','logical replication worker','walsender') "
            f"AND pid <> pg_catalog.pg_backend_pid() AND (usename IS NULL OR usename NOT IN ({matching_roles}))",
            'SELECT count(*) FROM pg_catalog.pg_prepared_xacts',
            'SELECT count(*) FROM pg_catalog.pg_subscription',
            "SELECT count(*) FROM sios_memory.async_operations WHERE status IS DISTINCT FROM 'completed'",
        )
        for query in queries:
            if self.connection.scalar(query) != '0':
                raise ValueError('database writes or Hindsight operations are still outstanding')

    def _prove_peer_configuration(self):
        hba, ident = production_access_files()
        expected_rules = [row.split() for row in hba.splitlines()]
        observed = self.connection.query(
            "SELECT rule_number, type, database::text, user_name::text, address, netmask, "
            "auth_method, options::text, error FROM pg_catalog.pg_hba_file_rules ORDER BY rule_number"
        )
        expected = []
        for index, fields in enumerate(expected_rules, 1):
            kind, database, role, *rest = fields
            address, mask = None, None
            if kind == 'host':
                prefix, bits = rest.pop(0).split('/')
                address = prefix
                mask = {'0.0.0.0': '0.0.0.0', '::': '::'}[prefix]
                assert bits == '0'
            method, *options = rest
            expected.append([
                str(index), kind, '{' + database.strip('"') + '}', '{' + role.strip('"') + '}',
                address, mask, method, '{' + ','.join(options) + '}' if options else None, None,
            ])
        mappings = self.connection.query(
            "SELECT map_name, sys_name, pg_username, error "
            "FROM pg_catalog.pg_ident_file_mappings ORDER BY map_number"
        )
        expected_mappings = [[*(field.strip('"') for field in line.split()), None]
                             for line in ident.splitlines()]
        if (observed != expected or mappings != expected_mappings
                or self.connection.scalar("SELECT current_setting('listen_addresses')") != ''):
            raise ValueError('PostgreSQL peer rules or listener settings did not match')

    def prove_recovery(self):
        """Verify the selected role and reject other sessions or background work."""
        with self._control():
            if self._configured_logins() != {HOST_ADMIN, RECOVERY_WRITER}:
                raise ValueError('the protected recovery role is not the only writer')
            self._prove_peer_configuration()
            self._prove_drained(RECOVERY_WRITER)

    def close(self):
        """Disable future writer connections before terminating existing writer sessions."""
        with self._control():
            self._configured_logins()
            self.connection.query(
                f'BEGIN; ALTER ROLE {APP_WRITER} NOLOGIN; ALTER ROLE {RECOVERY_WRITER} NOLOGIN; COMMIT'
            )
            results = self.connection.query(
                "SELECT pg_catalog.pg_terminate_backend(pid, 5000) FROM pg_catalog.pg_stat_activity "
                f"WHERE usename IN ('{APP_WRITER}','{RECOVERY_WRITER}')"
            )
            if any(row != ['t'] for row in results) or self._configured_logins() != {HOST_ADMIN}:
                raise ValueError('writer connection closure could not be verified')
            self._prove_drained()
            return 'closed'

    def open_after_restart(self, mode, previous_start):
        """Admit one peer only after a restart of the closed PostgreSQL instance."""
        role = WRITERS.get(mode)
        if role is None or not previous_start:
            raise ValueError('a writer mode and pre-restart startup timestamp are required')
        with self._control():
            if self._configured_logins() != {HOST_ADMIN}:
                raise ValueError('writer roles must already be closed')
            restarted = self.connection.scalar(
                'SELECT pg_catalog.pg_postmaster_start_time() > '
                + self.connection.literal(previous_start) + '::timestamptz'
            )
            if restarted != 't':
                raise ValueError('PostgreSQL must restart while writer roles are closed')
            self._prove_peer_configuration()
            self._prove_drained()
            self.connection.query(f'ALTER ROLE {role} LOGIN')
            if self._configured_logins() != {HOST_ADMIN, role}:
                raise ValueError('expected writer login was not verified')
            return mode
