"""Render the access policy for a protected, Unix-socket-only PostgreSQL service."""

from dataclasses import dataclass
import re


MAP = 'sios_os_identity'


@dataclass(frozen=True)
class Peer:
    operating_system_user: str
    database_role: str

    def __post_init__(self):
        for name in (self.operating_system_user, self.database_role):
            if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', name):
                raise ValueError('invalid local identity')


def access_files(*, bank_database, administrator, application, recovery):
    """Render configuration for an administrator and two mutually exclusive writers.

    Role login and active-session exclusion must be enforced independently.
    The service must use these files from a root-owned directory with no ACLs
    that grant untrusted write access, a private socket, and no TCP listener.
    """
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', bank_database):
        raise ValueError('invalid bank database name')
    peers = (administrator, application, recovery)
    if len({peer.operating_system_user for peer in peers}) != len(peers):
        raise ValueError('every peer must have a distinct OS identity')
    if len({peer.database_role for peer in peers}) != len(peers):
        raise ValueError('every peer must have a distinct PostgreSQL role')
    if 'root' in (application.operating_system_user, recovery.operating_system_user):
        raise ValueError('application and recovery must run unprivileged')

    # Quotes force literal matching even if an identifier is a pg_hba keyword.
    rules = [
        f'local all "{administrator.database_role}" peer map={MAP}',
        f'local "{bank_database}" "{application.database_role}" peer map={MAP}',
        f'local "{bank_database}" "{recovery.database_role}" peer map={MAP}',
        'local replication all reject',
        'local all all reject',
        'host replication all 0.0.0.0/0 reject',
        'host replication all ::/0 reject',
        'host all all 0.0.0.0/0 reject',
        'host all all ::/0 reject',
    ]
    identifiers = [
        f'{MAP} "{peer.operating_system_user}" "{peer.database_role}"' for peer in peers
    ]
    return '\n'.join(rules) + '\n', '\n'.join(identifiers) + '\n'


def production_access_files():
    return access_files(
        bank_database='sios_hindsight_migration',
        administrator=Peer('root', 'sios_host_admin'),
        application=Peer('_sios_hindsight', 'sios_app_writer'),
        recovery=Peer('_sios_recovery', 'sios_recovery_writer'),
    )
